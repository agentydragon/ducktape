"""TabbyAPI serving the staged EXL3 Flash-Next checkpoints on wyrm2."""

from cdk8s import ApiObjectMetadata
from cdk8s_plus_34 import ConfigMap, k8s
from constructs import Construct
from python.runfiles import Runfiles

from cluster.cdk8s.ollama.tabby_endpoint import MODEL_NAME, NAME, NAMESPACE, PORT, SWITCHING_SERVICE
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from model_catalog.catalog import TABBYAPI_QWEN_ABLITERATED

_NODE = "wyrm2"
_IMAGE = "ghcr.io/theroyallab/tabbyapi@sha256:6daf422856272925b8fa0db0e697b9cc7b68e5c557b2789fcf84d6e54139dfd6"
_ABLITERATED_MODEL = TABBYAPI_QWEN_ABLITERATED.upstream_model
assert _ABLITERATED_MODEL is not None
_CHECKPOINTS = (
    ("weights", MODEL_NAME, "tabbyapi-flash-next", "100Gi"),
    ("abliterated-weights", _ABLITERATED_MODEL, "tabbyapi-flash-next-abliterated", "61Gi"),
)
_PREFIX_CACHE = "tabbyapi-prefix-cache"
_PREFIX_CACHE_PATH = "/prefix-cache"
_HOSTNAME_LABEL = "kubernetes.io/hostname"
_SWITCHING_LABEL = {"ducktape.org/tabbyapi-model-switching": "true"}
_PODS = Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", NAME),))
SERVICE = ServiceRef(name=NAME, port=Port(name="http", number=PORT), pods=_PODS)
MODELS_PATH = "/v1/models"
_SOURCE_DIR = "ducktape/cluster/cdk8s/ollama"


def _local_claim(scope: Construct, path: str, claim: str, capacity: str) -> None:
    size = k8s.Quantity.from_string(capacity)
    volume = f"{_NODE}-{claim}"
    k8s.KubePersistentVolume(
        scope,
        f"{claim}-pv",
        metadata=k8s.ObjectMeta(name=volume),
        spec=k8s.PersistentVolumeSpec(
            access_modes=["ReadWriteOnce"],
            capacity={"storage": size},
            claim_ref=k8s.ObjectReference(name=claim, namespace=NAMESPACE),
            local=k8s.LocalVolumeSource(path=path),
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
        f"{claim}-pvc",
        metadata=k8s.ObjectMeta(name=claim, namespace=NAMESPACE),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="",
            volume_name=volume,
            resources=k8s.VolumeResourceRequirements(requests={"storage": size}),
        ),
    )


def _deploy(scope: Construct, config: ConfigMap) -> None:
    models = k8s.HttpGetAction(path=MODELS_PATH, port=k8s.IntOrString.from_string(SERVICE.port.name))
    k8s.KubeDeployment(
        scope,
        "tabbyapi-deployment",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=NAMESPACE,
            labels=_PODS.selector,
            # The config and template use subPath mounts and need a pod restart.
            annotations={"configmap.reloader.stakater.com/reload": config.name},
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=_PODS.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels={**_PODS.selector, **_SWITCHING_LABEL}),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    runtime_class_name="nvidia",
                    node_selector={_HOSTNAME_LABEL: _NODE},
                    security_context=k8s.PodSecurityContext(seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault")),
                    containers=[
                        k8s.Container(
                            name=NAME,
                            image=_IMAGE,
                            args=["tabbyapi_serve.py", "--host", "0.0.0.0"],
                            ports=[SERVICE.port.k8s_container_port()],
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
                            ),
                            env=[k8s.EnvVar(name="CUDA_DEVICE_ORDER", value="PCI_BUS_ID")],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("4"),
                                    "memory": k8s.Quantity.from_string("20Gi"),
                                },
                                limits={
                                    "memory": k8s.Quantity.from_string("48Gi"),
                                    "nvidia.com/gpu": k8s.Quantity.from_string("2"),
                                },
                            ),
                            startup_probe=k8s.Probe(
                                http_get=models, period_seconds=5, timeout_seconds=2, failure_threshold=72
                            ),
                            # Compilation can block the health handler during a first request.
                            readiness_probe=k8s.Probe(
                                http_get=models, period_seconds=10, timeout_seconds=2, failure_threshold=6
                            ),
                            volume_mounts=[
                                *[
                                    k8s.VolumeMount(name=volume, mount_path=f"/models/{name}", read_only=True)
                                    for volume, name, _, _ in _CHECKPOINTS
                                ],
                                k8s.VolumeMount(
                                    name="config", mount_path="/app/config.yml", sub_path="config.yml", read_only=True
                                ),
                                k8s.VolumeMount(
                                    name="config",
                                    mount_path="/app/tabbyapi_serve.py",
                                    sub_path="tabbyapi_serve.py",
                                    read_only=True,
                                ),
                                *[
                                    k8s.VolumeMount(
                                        name="config",
                                        mount_path=f"/models/{name}/chat_template.jinja",
                                        sub_path="chat_template.jinja",
                                        read_only=True,
                                    )
                                    for _, name, _, _ in _CHECKPOINTS
                                ],
                                k8s.VolumeMount(name=_PREFIX_CACHE, mount_path=_PREFIX_CACHE_PATH),
                                k8s.VolumeMount(name="shm", mount_path="/dev/shm"),
                            ],
                        )
                    ],
                    volumes=[
                        *[
                            k8s.Volume(
                                name=volume,
                                persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(
                                    claim_name=claim, read_only=True
                                ),
                            )
                            for volume, _, claim, _ in _CHECKPOINTS
                        ],
                        k8s.Volume(
                            name=_PREFIX_CACHE,
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=_PREFIX_CACHE),
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
            "tabbyapi_serve.py": (sources / "tabbyapi_serve.py").read_text(),
            "chat_template.jinja": (sources / "tabbyapi-chat-template.jinja").read_text(),
        },
    )
    for _, name, claim, capacity in _CHECKPOINTS:
        _local_claim(scope, f"/games/llm-models/{name}", claim, capacity)
    _local_claim(scope, "/games/tabbyapi-prefix-cache", _PREFIX_CACHE, "16Gi")
    _deploy(scope, config)
    k8s.KubeService(
        scope,
        "tabbyapi-service",
        metadata=k8s.ObjectMeta(name=SERVICE.name, namespace=NAMESPACE, labels=_PODS.selector),
        spec=k8s.ServiceSpec(selector=_PODS.selector, ports=[SERVICE.port.k8s_service_port()]),
    )
    # The older pod serves unknown model IDs with its loaded stock model. The
    # candidate route must only reach the new pod, even if LiteLLM rolls first.
    k8s.KubeService(
        scope,
        SWITCHING_SERVICE,
        metadata=k8s.ObjectMeta(name=SWITCHING_SERVICE, namespace=NAMESPACE, labels=_PODS.selector),
        spec=k8s.ServiceSpec(selector={**_PODS.selector, **_SWITCHING_LABEL}, ports=[SERVICE.port.k8s_service_port()]),
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
