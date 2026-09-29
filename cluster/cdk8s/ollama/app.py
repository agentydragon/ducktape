"""Ollama on the GPU node, behind an nginx bearer-token proxy, plus the RBAC and the one-shot
model bootstrap Job around it.

Hand-written beside the generated output: the `configMapGenerator` inputs in
cluster/k8s/ollama (`setup-gpt-oss-v2.sh`, the nginx config and template) and the
`kustomization.yaml` that generates them.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObject, ApiObjectMetadata, App, Chart, JsonPatch, Size
from cdk8s_plus_34 import (
    PersistentVolume,
    PersistentVolumeAccessMode,
    PersistentVolumeClaim,
    PersistentVolumeReclaimPolicy,
    k8s,
)
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)

from cluster.cdk8s import namespaces
from cluster.cdk8s.external_secrets.minted_secret import mint_bearer_secret
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/ollama"
_NAME = "ollama"
_NAMESPACE = "ollama"
_PODS = Pods(namespace=_NAMESPACE, labels=(("app.kubernetes.io/name", _NAME),))
# Ollama's own API, without auth, for in-cluster clients.
SERVICE = ServiceRef(name=_NAME, port=Port(name="http", number=11434), pods=_PODS)
# The bearer-checking nginx in front of it, which the public route targets.
_AUTH_PROXY = ServiceRef(name=_NAME, port=Port(name="auth-proxy", number=11435), pods=_PODS)
_MODELS_CLAIM = "llm-models"
_SSD_MODELS_CLAIM = "qwen38-iq4-ssd"
_SSD_MODELS_VOLUME = "wyrm2-qwen38-iq4-ssd"
_DIRECT_TOKEN = SecretRef(namespace=_NAMESPACE, name="ollama-direct-token").key("token")
# Both rendered by the hand-written kustomization.yaml's configMapGenerator.
_AUTH_PROXY_CONFIG_MAP = "ollama-auth-proxy"
_SCRIPTS_CONFIG_MAP = "gpt-oss-scripts"
# Pause this Deployment before exclusive host inference experiments.
_PAUSED_FOR_HOST_EXPERIMENTS = False


def _namespace(scope: Construct) -> None:
    namespaces.namespace(
        scope,
        "namespace",
        name=_NAMESPACE,
        # `auto` mode rewrites pod limits at admission time using the VPA's
        # idle-history recommendations. For a bursty LLM workload that sits at
        # ~50 MiB until a model loads (then needs tens of GiB) this caused the
        # ollama pod to ship with a 1 GiB memory limit and OOM on every model
        # load. Stay opted-in for recommendation reports, but don't let
        # goldilocks mutate pods.
        vpa=Vpa.RECOMMEND,
        agent_readable=None,
    )


def _models_claim(scope: Construct) -> None:
    k8s.KubePersistentVolumeClaim(
        scope,
        "models",
        metadata=k8s.ObjectMeta(name=_MODELS_CLAIM, namespace=_NAMESPACE),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            # OpenEBS LVM HDD on wyrm2 — co-located with GPUs. VG is 500GB
            # (proxmox-vms.tf); this PVC plus the 20Gi public-coder-devbox PVC are its
            # only other consumers. 350Gi covers the ~228GB roster
            # (setup-gpt-oss-v2.sh) with headroom for future additions.
            storage_class_name="lvm-proxmox-hdd",
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("350Gi")}),
        ),
    )


def _ssd_models(scope: Construct) -> None:
    # Existing host-managed ext4 filesystem, not a dynamically provisioned LVM LV.
    # Capacity advertises this directory; it is not a filesystem quota.
    volume = PersistentVolume(
        scope,
        "ssd-models-volume",
        metadata=ApiObjectMetadata(
            name=_SSD_MODELS_VOLUME, annotations={"kustomize.toolkit.fluxcd.io/prune": "disabled"}
        ),
        storage=Size.gibibytes(90),
        access_modes=[PersistentVolumeAccessMode.READ_WRITE_ONCE],
        reclaim_policy=PersistentVolumeReclaimPolicy.RETAIN,
        storage_class_name="",
    )
    # The fluent PV has no local source or node-affinity fields.
    ApiObject.of(volume).add_json_patch(
        JsonPatch.add(
            "/spec/local", k8s.LocalVolumeSource(path="/var/lib/llm-models-ssd/Qwen3.8-Flash-Next-GGUF/UD-IQ4_XS")
        ),
        JsonPatch.add(
            "/spec/nodeAffinity",
            k8s.VolumeNodeAffinity(
                required=k8s.NodeSelector(
                    node_selector_terms=[
                        k8s.NodeSelectorTerm(
                            match_expressions=[
                                k8s.NodeSelectorRequirement(
                                    key="kubernetes.io/hostname", operator="In", values=["wyrm2"]
                                )
                            ]
                        )
                    ]
                )
            ),
        ),
    )
    claim = PersistentVolumeClaim(
        scope,
        "ssd-models-claim",
        metadata=ApiObjectMetadata(
            name=_SSD_MODELS_CLAIM, namespace=_NAMESPACE, annotations={"kustomize.toolkit.fluxcd.io/prune": "disabled"}
        ),
        access_modes=[PersistentVolumeAccessMode.READ_WRITE_ONCE],
        storage_class_name="",
        storage=Size.gibibytes(90),
        volume=volume,
    )
    volume.bind(claim)
    # cdk8s-plus bind() renders only the name; PV claimRef also needs the namespace.
    ApiObject.of(volume).add_json_patch(JsonPatch.add("/spec/claimRef/namespace", _NAMESPACE))


def _ollama_container() -> k8s.Container:
    probe_action = k8s.HttpGetAction(path="/", port=k8s.IntOrString.from_string("ollama"))
    return k8s.Container(
        name="ollama",
        # renovate: datasource=docker
        image="ollama/ollama:0.34.4",
        ports=[k8s.ContainerPort(name="ollama", container_port=SERVICE.pod_port, protocol="TCP")],
        env=[
            k8s.EnvVar(name="OLLAMA_MODELS", value="/models"),
            # SSD blobs are externally managed read-only symlinks. Keep startup GC
            # from unlinking them before the bootstrap Job registers the model.
            k8s.EnvVar(name="OLLAMA_NOPRUNE", value="true"),
            k8s.EnvVar(name="OLLAMA_NUM_PARALLEL", value="1"),
            k8s.EnvVar(name="OLLAMA_MAX_LOADED_MODELS", value="1"),
            # This NVIDIA-only service uses CUDA in PCI bus order. Verify the
            # child runner's CUDA_VISIBLE_DEVICES before interpreting fit margins.
            k8s.EnvVar(name="OLLAMA_VULKAN", value="false"),
            k8s.EnvVar(name="CUDA_DEVICE_ORDER", value="PCI_BUS_ID"),
            # 2/0 GiB passed short requests but GPU1 OOMed during 145K prefill.
            # Leave runtime allocation room on both GPUs plus desktop headroom.
            k8s.EnvVar(name="LLAMA_ARG_FIT_TARGET", value="4096,2048"),
            k8s.EnvVar(name="OLLAMA_HOST", value=f"0.0.0.0:{SERVICE.pod_port}"),
            k8s.EnvVar(name="NVIDIA_VISIBLE_DEVICES", value="all"),
            k8s.EnvVar(name="OLLAMA_KV_CACHE_TYPE", value="q8_0"),
            k8s.EnvVar(name="OLLAMA_FLASH_ATTENTION", value="1"),
            k8s.EnvVar(name="OLLAMA_CONTEXT_LENGTH", value="131072"),
            # Default 5m is shorter than a cold read of the 112GB qwen3.8-flash-next-q4
            # weights off HDD-backed lvm-proxmox-hdd; Ollama abandons the load attempt
            # (and does not retry) once this elapses.
            k8s.EnvVar(name="OLLAMA_LOAD_TIMEOUT", value="30m"),
        ],
        resources=k8s.ResourceRequirements(
            requests={
                "nvidia.com/gpu": k8s.Quantity.from_number(2),
                "memory": k8s.Quantity.from_string("24Gi"),
                "cpu": k8s.Quantity.from_string("2"),
            },
            limits={"nvidia.com/gpu": k8s.Quantity.from_number(2), "memory": k8s.Quantity.from_string("40Gi")},
        ),
        volume_mounts=[
            k8s.VolumeMount(name="models", mount_path="/models"),
            k8s.VolumeMount(name="ssd-models", mount_path="/ssd-models", read_only=True),
        ],
        liveness_probe=k8s.Probe(http_get=probe_action, initial_delay_seconds=30, period_seconds=30),
        readiness_probe=k8s.Probe(http_get=probe_action, initial_delay_seconds=10, period_seconds=10),
    )


def _auth_proxy_container() -> k8s.Container:
    return k8s.Container(
        name="auth-proxy",
        # renovate: datasource=docker
        image="nginx:1.31-alpine",
        ports=[_AUTH_PROXY.port.k8s_container_port()],
        env=[_DIRECT_TOKEN.env_var("OLLAMA_DIRECT_TOKEN")],
        volume_mounts=[
            k8s.VolumeMount(name="nginx-templates", mount_path="/etc/nginx/templates"),
            k8s.VolumeMount(name="nginx-config", mount_path="/etc/nginx/nginx.conf", sub_path="nginx.conf"),
        ],
        liveness_probe=k8s.Probe(
            tcp_socket=k8s.TcpSocketAction(port=k8s.IntOrString.from_string(_AUTH_PROXY.port.name)),
            initial_delay_seconds=5,
            period_seconds=30,
        ),
    )


def _deployment(scope: Construct) -> None:
    k8s.KubeDeployment(
        scope,
        "deployment",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=_NAMESPACE, labels=_PODS.selector),
        spec=k8s.DeploymentSpec(
            replicas=0 if _PAUSED_FOR_HOST_EXPERIMENTS else 1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=_PODS.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_PODS.selector),
                spec=k8s.PodSpec(
                    runtime_class_name="nvidia",
                    node_selector={"feature.node.kubernetes.io/pci-10de.present": "true"},
                    tolerations=[k8s.Toleration(key="nvidia.com/gpu", operator="Exists", effect="PreferNoSchedule")],
                    init_containers=[
                        k8s.Container(
                            name="link-ssd-models",
                            # renovate: datasource=docker
                            image="ollama/ollama:0.34.4",
                            command=["/bin/sh", "/scripts/link-ssd-models.sh"],
                            volume_mounts=[
                                k8s.VolumeMount(name="models", mount_path="/models"),
                                k8s.VolumeMount(name="ssd-models", mount_path="/ssd-models", read_only=True),
                                k8s.VolumeMount(name="scripts", mount_path="/scripts", read_only=True),
                            ],
                        )
                    ],
                    containers=[_ollama_container(), _auth_proxy_container()],
                    volumes=[
                        k8s.Volume(
                            name="ssd-models",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(
                                claim_name=_SSD_MODELS_CLAIM, read_only=True
                            ),
                        ),
                        k8s.Volume(name="scripts", config_map=k8s.ConfigMapVolumeSource(name=_SCRIPTS_CONFIG_MAP)),
                        k8s.Volume(
                            name="models",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=_MODELS_CLAIM),
                        ),
                        k8s.Volume(
                            name="nginx-templates", config_map=k8s.ConfigMapVolumeSource(name=_AUTH_PROXY_CONFIG_MAP)
                        ),
                        k8s.Volume(
                            name="nginx-config", config_map=k8s.ConfigMapVolumeSource(name=_AUTH_PROXY_CONFIG_MAP)
                        ),
                    ],
                ),
            ),
        ),
    )


def _service(scope: Construct) -> None:
    k8s.KubeService(
        scope,
        "service",
        metadata=k8s.ObjectMeta(name=SERVICE.name, namespace=_NAMESPACE, labels={"app": "ollama"}),
        spec=k8s.ServiceSpec(
            type="ClusterIP",
            selector=_PODS.selector,
            ports=[SERVICE.port.k8s_service_port(), _AUTH_PROXY.port.k8s_service_port()],
        ),
    )


def _rbac(scope: Construct) -> None:
    k8s.KubeClusterRole(
        scope,
        "api-consumer",
        metadata=k8s.ObjectMeta(name="ollama-api-consumer"),
        rules=[
            k8s.PolicyRule(
                api_groups=[""],
                resources=["secrets"],
                resource_names=["litellm-master-key", _DIRECT_TOKEN.secret.name],
                verbs=["get"],
            )
        ],
    )
    k8s.KubeRole(
        scope,
        "reader",
        metadata=k8s.ObjectMeta(name="ollama-reader", namespace=_NAMESPACE),
        rules=[
            k8s.PolicyRule(
                api_groups=[""],
                resources=["pods", "pods/log", "pods/portforward", "services", "configmaps", "events"],
                verbs=["get", "list", "watch", "create"],
            ),
            k8s.PolicyRule(
                api_groups=["infra.contrib.fluxcd.io"], resources=["terraforms"], verbs=["get", "list", "watch"]
            ),
        ],
    )
    k8s.KubeRoleBinding(
        scope,
        "reader-binding",
        metadata=k8s.ObjectMeta(name="claude-ollama-reader", namespace=_NAMESPACE),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name="ollama-reader"),
        subjects=[
            k8s.Subject(kind="ServiceAccount", name="default", namespace="claude-sandbox"),
            k8s.Subject(
                kind="Group", name="oidc-ksbx-groups:kubectl-sandbox-users", api_group="rbac.authorization.k8s.io"
            ),
        ],
    )


def _setup_job(scope: Construct) -> None:
    k8s.KubeJob(
        scope,
        "setup-gpt-oss",
        # Explicit version bumps own reruns; Reloader would delete a running Job
        # when the scripts ConfigMap changes.
        metadata=k8s.ObjectMeta(
            name="setup-gpt-oss-v8", namespace=_NAMESPACE, annotations={"reloader.stakater.com/auto": "false"}
        ),
        spec=k8s.JobSpec(
            template=k8s.PodTemplateSpec(
                spec=k8s.PodSpec(
                    restart_policy="OnFailure",
                    node_selector={"kubernetes.io/hostname": "wyrm2"},
                    volumes=[
                        k8s.Volume(
                            name="scripts",
                            config_map=k8s.ConfigMapVolumeSource(name=_SCRIPTS_CONFIG_MAP, default_mode=0o755),
                        ),
                        k8s.Volume(
                            name="models",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=_MODELS_CLAIM),
                        ),
                        k8s.Volume(
                            name="ssd-models",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=_SSD_MODELS_CLAIM),
                        ),
                    ],
                    init_containers=[
                        k8s.Container(
                            name="link-ssd-models",
                            # renovate: datasource=docker
                            image="ollama/ollama:0.34.4",
                            command=["/bin/sh", "/scripts/link-ssd-models.sh"],
                            volume_mounts=[
                                k8s.VolumeMount(name="models", mount_path="/models"),
                                k8s.VolumeMount(name="ssd-models", mount_path="/ssd-models", read_only=True),
                                k8s.VolumeMount(name="scripts", mount_path="/scripts", read_only=True),
                            ],
                        ),
                        # Native sidecar exits when the setup container finishes.
                        # Import touches blob mtimes, requiring a writable mount here.
                        # No GPU resources/runtime and no generation requests: the
                        # serving Deployment keeps its SSD mount read-only.
                        k8s.Container(
                            name="registration-api",
                            # renovate: datasource=docker
                            image="ollama/ollama:0.34.4",
                            args=["serve"],
                            restart_policy="Always",
                            env=[
                                k8s.EnvVar(name="OLLAMA_HOST", value="127.0.0.1:11434"),
                                k8s.EnvVar(name="OLLAMA_MODELS", value="/models"),
                                k8s.EnvVar(name="OLLAMA_NOPRUNE", value="true"),
                                k8s.EnvVar(name="OLLAMA_NO_CLOUD", value="true"),
                            ],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("100m"),
                                    "memory": k8s.Quantity.from_string("128Mi"),
                                },
                                limits={"cpu": k8s.Quantity.from_number(1), "memory": k8s.Quantity.from_string("1Gi")},
                            ),
                            volume_mounts=[
                                k8s.VolumeMount(name="models", mount_path="/models"),
                                k8s.VolumeMount(name="ssd-models", mount_path="/ssd-models"),
                            ],
                        ),
                    ],
                    containers=[
                        k8s.Container(
                            name="setup",
                            # renovate: datasource=docker
                            image="curlimages/curl:8.22.0",
                            command=["/scripts/setup-gpt-oss-v2.sh"],
                            env=[k8s.EnvVar(name="OLLAMA_HOST", value="http://127.0.0.1:11434")],
                            volume_mounts=[k8s.VolumeMount(name="scripts", mount_path="/scripts", read_only=True)],
                        )
                    ],
                )
            )
        ),
    )


def _direct_token(scope: Construct) -> None:
    """ESO owns a fresh direct-Ollama bearer token. The separate name makes this cutover
    safe: Flux can prune the Terraform-owned Secret only after every consumer has switched
    to this target."""
    mint_bearer_secret(
        scope,
        "direct-token",
        name=_DIRECT_TOKEN.secret.name,
        namespace=_DIRECT_TOKEN.secret.namespace,
        key=_DIRECT_TOKEN.key,
        # A direct-API credential is generated once, not periodically rotated.
        refresh="8760h",
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        target_annotations={
            "reflector.v1.k8s.emberstack.com/reflection-allowed": "true",
            "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces": "claude-sandbox",
            "reflector.v1.k8s.emberstack.com/reflection-auto-enabled": "true",
            "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces": "claude-sandbox",
        },
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    _namespace(chart)
    _models_claim(chart)
    _ssd_models(chart)
    _deployment(chart)
    _service(chart)
    https_route(
        chart,
        "route",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        hostnames=["ollama.allegedly.works"],
        backend=_AUTH_PROXY,
        timeout="600s",
        hsts=False,
        listener=None,
    )
    _rbac(chart)
    if not _PAUSED_FOR_HOST_EXPERIMENTS:
        _setup_job(chart)
    _direct_token(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
