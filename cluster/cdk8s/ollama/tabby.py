"""TabbyAPI serving the staged EXL3 Flash-Next checkpoint on wyrm2."""

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ConfigMap, k8s
from constructs import Construct
from python.runfiles import Runfiles

from cluster.cdk8s.ollama.tabby_endpoint import MODEL_NAME, NAME, NAMESPACE, PORT
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

_NODE = "wyrm2"
_IMAGE = "ghcr.io/theroyallab/tabbyapi@sha256:6daf422856272925b8fa0db0e697b9cc7b68e5c557b2789fcf84d6e54139dfd6"
_MODEL_PATH = f"/models/{MODEL_NAME}"
_VOLUME = "wyrm2-tabbyapi-flash-next"
_CLAIM = "tabbyapi-flash-next"
_HOSTNAME_LABEL = "kubernetes.io/hostname"
_PODS = Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", NAME),))
SERVICE = ServiceRef(name=NAME, port=Port(name="http", number=PORT), pods=_PODS)
MODELS_PATH = "/v1/models"
_SOURCE_DIR = "ducktape/cluster/cdk8s/ollama"


def _weights(scope: Construct) -> None:
    size = k8s.Quantity.from_string("100Gi")
    k8s.KubePersistentVolume(
        scope,
        "tabbyapi-pv",
        metadata=k8s.ObjectMeta(name=_VOLUME),
        spec=k8s.PersistentVolumeSpec(
            access_modes=["ReadWriteOnce"],
            capacity={"storage": size},
            claim_ref=k8s.ObjectReference(name=_CLAIM, namespace=NAMESPACE),
            # The pre-staged checkpoint is a read-only cache on the wyrm2 NVMe filesystem.
            local=k8s.LocalVolumeSource(path=f"/games/llm-models/{MODEL_NAME}"),
            node_affinity=k8s.VolumeNodeAffinity(
                required=k8s.NodeSelector(
                    node_selector_terms=[
                        k8s.NodeSelectorTerm(
                            match_expressions=[
                                k8s.NodeSelectorRequirement(key=_HOSTNAME_LABEL, operator="In", values=[_NODE])
                            ]
                        )
                    ]
                )
            ),
            persistent_volume_reclaim_policy="Retain",
            storage_class_name="",
        ),
    )
    k8s.KubePersistentVolumeClaim(
        scope,
        "tabbyapi-pvc",
        metadata=k8s.ObjectMeta(name=_CLAIM, namespace=NAMESPACE),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="",
            volume_name=_VOLUME,
            resources=k8s.VolumeResourceRequirements(requests={"storage": size}),
        ),
    )


def _deploy(scope: Construct, config: ConfigMap) -> None:
    models = k8s.HttpGetAction(path=MODELS_PATH, port=k8s.IntOrString.from_string(SERVICE.port.name))
    k8s.KubeDeployment(
        scope,
        "tabbyapi-deployment",
        metadata=k8s.ObjectMeta(name=NAME, namespace=NAMESPACE, labels=_PODS.selector),
        spec=k8s.DeploymentSpec(
            replicas=1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=_PODS.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_PODS.selector),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    runtime_class_name="nvidia",
                    node_selector={_HOSTNAME_LABEL: _NODE},
                    security_context=k8s.PodSecurityContext(seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")),
                    containers=[
                        k8s.Container(
                            name=NAME,
                            image=_IMAGE,
                            ports=[SERVICE.port.k8s_container_port()],
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
                            ),
                            env=[k8s.EnvVar(name="CUDA_DEVICE_ORDER", value="PCI_BUS_ID")],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("4"),
                                    "memory": k8s.Quantity.from_string("12Gi"),
                                },
                                limits={
                                    "memory": k8s.Quantity.from_string("48Gi"),
                                    "nvidia.com/gpu": k8s.Quantity.from_string("2"),
                                },
                            ),
                            startup_probe=k8s.Probe(
                                http_get=models, period_seconds=5, timeout_seconds=2, failure_threshold=72
                            ),
                            readiness_probe=k8s.Probe(http_get=models, period_seconds=10, timeout_seconds=2),
                            volume_mounts=[
                                k8s.VolumeMount(name="weights", mount_path=_MODEL_PATH, read_only=True),
                                k8s.VolumeMount(
                                    name="config", mount_path="/app/config.yml", sub_path="config.yml", read_only=True
                                ),
                                k8s.VolumeMount(
                                    name="config",
                                    mount_path=f"{_MODEL_PATH}/chat_template.jinja",
                                    sub_path="chat_template.jinja",
                                    read_only=True,
                                ),
                                k8s.VolumeMount(name="shm", mount_path="/dev/shm"),
                            ],
                        )
                    ],
                    volumes=[
                        k8s.Volume(
                            name="weights",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(
                                claim_name=_CLAIM, read_only=True
                            ),
                        ),
                        k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=config.name)),
                        k8s.Volume(
                            name="shm",
                            empty_dir=k8s.EmptyDirVolumeSource(
                                medium="Memory", size_limit=k8s.Quantity.from_string("8Gi")
                            ),
                        ),
                    ],
                ),
            ),
        ),
    )


def add_tabbyapi(scope: Construct) -> None:
    sources = Runfiles.CreateOrRaise().root() / _SOURCE_DIR
    config = ConfigMap(
        scope,
        "tabbyapi-config",
        metadata=ApiObjectMetadata(name=f"{NAME}-config", namespace=NAMESPACE),
        data={
            "config.yml": (sources / "tabbyapi-config.yml").read_text(),
            "chat_template.jinja": (sources / "tabbyapi-chat-template.jinja").read_text(),
        },
    )
    _weights(scope)
    _deploy(scope, config)
    k8s.KubeService(
        scope,
        "tabbyapi-service",
        metadata=k8s.ObjectMeta(name=SERVICE.name, namespace=NAMESPACE, labels=_PODS.selector),
        spec=k8s.ServiceSpec(selector=_PODS.selector, ports=[SERVICE.port.k8s_service_port()]),
    )
    # LiteLLM authenticates client requests. Gatus only needs the model-list health check.
    k8s.KubeNetworkPolicy(
        scope,
        "tabbyapi-ingress",
        metadata=k8s.ObjectMeta(name=f"{NAME}-ingress", namespace=NAMESPACE),
        spec=k8s.NetworkPolicySpec(
            pod_selector=k8s.LabelSelector(match_labels=_PODS.selector),
            policy_types=["Ingress"],
            ingress=[
                k8s.NetworkPolicyIngressRule(
                    from_=[
                        k8s.NetworkPolicyPeer(
                            namespace_selector=k8s.LabelSelector(match_labels={"kubernetes.io/metadata.name": name}),
                            pod_selector=k8s.LabelSelector(match_labels={"app.kubernetes.io/name": name}),
                        )
                        for name in ("litellm", "gatus")
                    ],
                    ports=[k8s.NetworkPolicyPort(port=k8s.IntOrString.from_number(SERVICE.pod_port), protocol="TCP")],
                )
            ],
        ),
    )
