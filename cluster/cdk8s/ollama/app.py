"""Ollama on the GPU node, behind an nginx bearer-token proxy, plus the RBAC and the one-shot
model bootstrap Job around it.

Hand-written beside the generated output: the `configMapGenerator` inputs in
cluster/k8s/ollama (`setup-gpt-oss-v2.sh`, the nginx config and template) and the
`kustomization.yaml` that generates them.
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from constructs import Construct
from eso_password_generator_crds.io.external_secrets.generators import Password, PasswordSpec
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateMetadata,
)

from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.providers.external_secrets.external_secret import DataFrom, ExternalSecret

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/ollama"
_NAME = "ollama"
_NAMESPACE = "ollama"
_LABELS = {"app.kubernetes.io/name": _NAME}
_OLLAMA_PORT = 11434
_AUTH_PROXY_PORT = 11435
_MODELS_CLAIM = "llm-models"
_DIRECT_TOKEN = "ollama-direct-token"
# Both rendered by the hand-written kustomization.yaml's configMapGenerator.
_AUTH_PROXY_CONFIG_MAP = "ollama-auth-proxy"
_SCRIPTS_CONFIG_MAP = "gpt-oss-scripts"
# Host inference experiments own the GPUs; retain models and routing for resumption.
_PAUSED_FOR_HOST_EXPERIMENTS = True


def _namespace(scope: Construct) -> None:
    k8s.KubeNamespace(
        scope,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=_NAMESPACE,
            labels={
                "goldilocks.fairwinds.com/enabled": "true",
                # `auto` mode rewrites pod limits at admission time using the VPA's
                # idle-history recommendations. For a bursty LLM workload that sits at
                # ~50 MiB until a model loads (then needs tens of GiB) this caused the
                # ollama pod to ship with a 1 GiB memory limit and OOM on every model
                # load. Stay opted-in for recommendation reports, but don't let
                # goldilocks mutate pods.
                "goldilocks.fairwinds.com/vpa-update-mode": "off",
            },
        ),
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


def _ollama_container() -> k8s.Container:
    probe_action = k8s.HttpGetAction(path="/", port=k8s.IntOrString.from_string("ollama"))
    return k8s.Container(
        name="ollama",
        image="ollama/ollama:0.34.4",
        ports=[k8s.ContainerPort(name="ollama", container_port=_OLLAMA_PORT, protocol="TCP")],
        env=[
            k8s.EnvVar(name="OLLAMA_MODELS", value="/models"),
            k8s.EnvVar(name="OLLAMA_HOST", value=f"0.0.0.0:{_OLLAMA_PORT}"),
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
        volume_mounts=[k8s.VolumeMount(name="models", mount_path="/models")],
        liveness_probe=k8s.Probe(http_get=probe_action, initial_delay_seconds=30, period_seconds=30),
        readiness_probe=k8s.Probe(http_get=probe_action, initial_delay_seconds=10, period_seconds=10),
    )


def _auth_proxy_container() -> k8s.Container:
    return k8s.Container(
        name="auth-proxy",
        image="nginx:1.31-alpine",
        ports=[k8s.ContainerPort(name="auth-proxy", container_port=_AUTH_PROXY_PORT, protocol="TCP")],
        env=[
            k8s.EnvVar(
                name="OLLAMA_DIRECT_TOKEN",
                value_from=k8s.EnvVarSource(secret_key_ref=k8s.SecretKeySelector(name=_DIRECT_TOKEN, key="token")),
            )
        ],
        volume_mounts=[
            k8s.VolumeMount(name="nginx-templates", mount_path="/etc/nginx/templates"),
            k8s.VolumeMount(name="nginx-config", mount_path="/etc/nginx/nginx.conf", sub_path="nginx.conf"),
        ],
        liveness_probe=k8s.Probe(
            tcp_socket=k8s.TcpSocketAction(port=k8s.IntOrString.from_string("auth-proxy")),
            initial_delay_seconds=5,
            period_seconds=30,
        ),
    )


def _deployment(scope: Construct) -> None:
    k8s.KubeDeployment(
        scope,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=_NAME, namespace=_NAMESPACE, labels=_LABELS, annotations={"reloader.stakater.com/auto": "true"}
        ),
        spec=k8s.DeploymentSpec(
            replicas=0 if _PAUSED_FOR_HOST_EXPERIMENTS else 1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    runtime_class_name="nvidia",
                    node_selector={"feature.node.kubernetes.io/pci-10de.present": "true"},
                    tolerations=[k8s.Toleration(key="nvidia.com/gpu", operator="Exists", effect="PreferNoSchedule")],
                    containers=[_ollama_container(), _auth_proxy_container()],
                    volumes=[
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
        metadata=k8s.ObjectMeta(name=_NAME, namespace=_NAMESPACE, labels={"app": "ollama"}),
        spec=k8s.ServiceSpec(
            type="ClusterIP",
            selector=_LABELS,
            ports=[
                k8s.ServicePort(
                    name="http", port=_OLLAMA_PORT, target_port=k8s.IntOrString.from_string("ollama"), protocol="TCP"
                ),
                k8s.ServicePort(
                    name="auth-proxy",
                    port=_AUTH_PROXY_PORT,
                    target_port=k8s.IntOrString.from_string("auth-proxy"),
                    protocol="TCP",
                ),
            ],
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
                resource_names=["litellm-master-key", _DIRECT_TOKEN],
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
        # Versioned so a changed bootstrap model list creates a fresh Job.
        metadata=k8s.ObjectMeta(name="setup-gpt-oss-v4", namespace=_NAMESPACE),
        spec=k8s.JobSpec(
            ttl_seconds_after_finished=86400,
            template=k8s.PodTemplateSpec(
                spec=k8s.PodSpec(
                    restart_policy="OnFailure",
                    node_selector={"topology.kubernetes.io/region": "proxmox"},
                    volumes=[
                        k8s.Volume(
                            name="scripts",
                            config_map=k8s.ConfigMapVolumeSource(name=_SCRIPTS_CONFIG_MAP, default_mode=0o755),
                        )
                    ],
                    containers=[
                        k8s.Container(
                            name="setup",
                            # curl + busybox awk; we hit /api/pull directly (no ollama CLI needed).
                            image="curlimages/curl:8.22.0",
                            command=["/scripts/setup-gpt-oss-v2.sh"],
                            env=[
                                k8s.EnvVar(
                                    name="OLLAMA_HOST", value=f"http://ollama.ollama.svc.cluster.local:{_OLLAMA_PORT}"
                                )
                            ],
                            volume_mounts=[k8s.VolumeMount(name="scripts", mount_path="/scripts")],
                        )
                    ],
                )
            ),
        ),
    )


def _direct_token(scope: Construct) -> None:
    """ESO owns a fresh direct-Ollama bearer token. The separate name makes this cutover
    safe: Flux can prune the Terraform-owned Secret only after every consumer has switched
    to this target."""
    generator = Password(
        scope,
        "direct-token-generator",
        metadata=metadata(_DIRECT_TOKEN, _NAMESPACE),
        spec=PasswordSpec(length=48, digits=12, symbols=0, no_upper=False, allow_repeat=True),
    )
    ExternalSecret(
        scope,
        "direct-token",
        name=_DIRECT_TOKEN,
        namespace=_NAMESPACE,
        # A direct-API credential is generated once, not periodically rotated.
        refresh="8760h",
        data_from=[DataFrom.from_password_generator(generator.name)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        template=ExternalSecretSpecTargetTemplate(
            type="Opaque",
            metadata=ExternalSecretSpecTargetTemplateMetadata(
                annotations={
                    "reflector.v1.k8s.emberstack.com/reflection-allowed": "true",
                    "reflector.v1.k8s.emberstack.com/reflection-allowed-namespaces": "claude-sandbox",
                    "reflector.v1.k8s.emberstack.com/reflection-auto-enabled": "true",
                    "reflector.v1.k8s.emberstack.com/reflection-auto-namespaces": "claude-sandbox",
                }
            ),
            data={"token": "{{ .password }}"},
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    _namespace(chart)
    _models_claim(chart)
    _deployment(chart)
    _service(chart)
    https_route(
        chart,
        "route",
        metadata=metadata(_NAME, _NAMESPACE),
        hostname="ollama.allegedly.works",
        backend=_NAME,
        port=_AUTH_PROXY_PORT,
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
