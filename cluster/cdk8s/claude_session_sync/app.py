"""claude-session-sync: a replicated web Deployment plus a single credential-owning control Deployment, its
CNPG database, the control PVC holding the OAuth credential, pull credentials and egress policies.

The credential is minted by `pair` inside the running container, not provisioned here
(devinfra/claude/session_export/docs/deploy.md): the Deployment starts unpaired and waits.

The one hand-written file is the `PINS_DIR` Component, which the kustomization includes across the roots: its
image-automation marker overrides this chart's placeholder image tag (cluster/cdk8s/AGENTS.md § the `:tag`
Setters marker).
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s import cilium, cnpg, forgejo_images, namespaces, node_scheduling, pod_policy
from cluster.cdk8s.fleet_rules import add_fleet_rules
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, IngressRule, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from devinfra.claude.session_export.settings import ControlSettings, WebSettings
from util.settings_contract import env_name

NAME = "claude-session-sync"
NAMESPACE = "claude-session-sync"
OUTPUT_DIR = f"{GENERATED_ROOT}/claude-session-sync"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/claude-session-sync-image-pins"
DATABASE = cnpg.PostgresRef.generated(name="claude-session-sync-db", namespace=NAMESPACE)
# The tag comes from the Component in PINS_DIR.
_IMAGE = "git.allegedly.works/ducktape-ci/claude-session-sync:unset"
# tf/gitops/sso-providers/provider_claude_session_sync.tf spells the same origin as its redirect URI.
HOSTNAME = "claude-session-sync.allegedly.works"
# Reflected from the authentik namespace; its keys are the OIDC environment variables of `WebSettings`.
_OIDC = SecretRef(namespace=NAMESPACE, name="claude-session-sync-oidc")
_OIDC_FIELDS = ("oidc_issuer", "oidc_client_id", "oidc_client_secret", "oidc_session_secret", "oidc_allowed_subject")
_DATA_CLAIM = "claude-session-sync-data"
_DATA_DIR = "/data"
_CREDENTIALS_FILE = f"{_DATA_DIR}/credentials.json"
_CONTROL_NAME = f"{NAME}-control"
# The hosts the OAuth token is refreshed at and the sessions API is read from.
_ANTHROPIC_HOSTS = ("api.anthropic.com", "platform.claude.com")
_UID = 1000
_WEB_LABELS = {"app.kubernetes.io/name": NAME, "app.kubernetes.io/component": "web"}
_CONTROL_LABELS = {"app.kubernetes.io/name": _CONTROL_NAME, "app.kubernetes.io/component": "control"}
_WEB_ENDPOINT_LABELS = {**cilium.endpoint_labels(NAMESPACE, NAME), "app.kubernetes.io/component": "web"}
_CONTROL_ENDPOINT_LABELS = {
    **cilium.endpoint_labels(NAMESPACE, _CONTROL_NAME),
    "app.kubernetes.io/component": "control",
}
WEB_SERVICE = ServiceRef(
    name=NAME, port=Port(name="http", number=8080), pods=Pods(namespace=NAMESPACE, labels=tuple(_WEB_LABELS.items()))
)
CONTROL_SERVICE = ServiceRef(
    name=_CONTROL_NAME,
    port=Port(name="http", number=8080),
    pods=Pods(namespace=NAMESPACE, labels=tuple(_CONTROL_LABELS.items())),
)


def _database(chart: Chart) -> None:
    cnpg.cluster(
        chart,
        "database",
        ref=DATABASE,
        annotations={
            "description": (
                "Events of every Claude Code cloud session, mirrored by claude-session-sync. Once the sessions "
                "age out of Anthropic's API this may be the only copy."
            )
        },
        placement=node_scheduling.HIL_OVH,
        storage_class="local-path-ovh-hdd",
        size="40Gi",
        initdb=cnpg.same_owner_initdb("claude_sessions"),
        wal_archive=False,
    )


def _data_claim(chart: Chart) -> None:
    # The OAuth credential the sync refreshes in place. Refresh tokens rotate, so this Deployment is the
    # credential's one owner (Recreate strategy, one replica); losing the file means pairing again.
    k8s.KubePersistentVolumeClaim(
        chart,
        "data",
        metadata=k8s.ObjectMeta(name=_DATA_CLAIM, namespace=NAMESPACE),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="seaweedfs-ovh",
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("1Gi")}),
        ),
    )


def _healthz(service: ServiceRef, *, initial_delay_seconds: int, period_seconds: int) -> k8s.Probe:
    return k8s.Probe(
        http_get=k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_number(service.pod_port)),
        initial_delay_seconds=initial_delay_seconds,
        period_seconds=period_seconds,
    )


def _deployment(
    chart: Chart,
    *,
    workload_id: str,
    name: str,
    labels: dict[str, str],
    selector_labels: dict[str, str] | None = None,
    args: list[str],
    env: list[k8s.EnvVar],
    service: ServiceRef,
    replicas: int,
    strategy: str,
    description: str,
    volumes: list[k8s.Volume] | None = None,
    volume_mounts: list[k8s.VolumeMount] | None = None,
    tolerate_control_plane: bool = False,
) -> None:
    deployment = k8s.KubeDeployment(
        chart,
        workload_id,
        metadata=k8s.ObjectMeta(
            name=name, namespace=NAMESPACE, labels=labels, annotations={"description": description}
        ),
        spec=k8s.DeploymentSpec(
            replicas=replicas,
            selector=k8s.LabelSelector(match_labels=selector_labels or labels),
            strategy=k8s.DeploymentStrategy(type=strategy),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=labels),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    image_pull_secrets=[k8s.LocalObjectReference(name=forgejo_images.SECRET_NAME)],
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True, run_as_user=_UID, run_as_group=_UID, fs_group=_UID
                    ),
                    containers=[
                        k8s.Container(
                            name=name,
                            image=_IMAGE,
                            args=args,
                            ports=[service.port.k8s_container_port()],
                            env=env,
                            readiness_probe=_healthz(service, initial_delay_seconds=5, period_seconds=10),
                            liveness_probe=_healthz(service, initial_delay_seconds=30, period_seconds=20),
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("50m"),
                                    "memory": k8s.Quantity.from_string("256Mi"),
                                },
                                # A page of 500 events, three sessions at once; one event alone reaches 10 MB.
                                limits={"memory": k8s.Quantity.from_string("1Gi")},
                            ),
                            # The aspect py_binary launcher materializes its venv under the image runfiles
                            # directory at startup, so the root filesystem stays writable.
                            security_context=k8s.SecurityContext(read_only_root_filesystem=False),
                            volume_mounts=volume_mounts,
                        )
                    ],
                    volumes=volumes,
                ),
            ),
        ),
    )
    pod_policy.harden(deployment)
    # The descheduler evicts a pod on a contended worker roughly every 15 minutes
    # (cluster/cdk8s/cli_proxy_api/cli_proxy_api.py); the credential owner is allowed control planes as overflow.
    pod_policy.place(deployment, node_scheduling.HIL_OVH, tolerate_control_plane=tolerate_control_plane)


def _web_deployment(chart: Chart) -> None:
    _deployment(
        chart,
        workload_id="web-deployment",
        name=NAME,
        labels=_WEB_LABELS,
        selector_labels={"app.kubernetes.io/name": NAME},  # preserve the existing Deployment's immutable selector
        args=["web"],
        service=WEB_SERVICE,
        replicas=2,
        strategy="RollingUpdate",
        description="Serves the owner-authenticated Claude session page and proxies controls to the credential owner.",
        env=[
            DATABASE.app_secret.key("uri").env_var(env_name(WebSettings, "database_url")),
            k8s.EnvVar(name=env_name(WebSettings, "public_base_url"), value=f"https://{HOSTNAME}"),
            k8s.EnvVar(name=env_name(WebSettings, "control_base_url"), value=CONTROL_SERVICE.url),
            k8s.EnvVar(name=env_name(WebSettings, "port"), value=str(WEB_SERVICE.pod_port)),
            *(_OIDC.key(env_name(WebSettings, field)).env_var(env_name(WebSettings, field)) for field in _OIDC_FIELDS),
        ],
    )


def _control_deployment(chart: Chart) -> None:
    _deployment(
        chart,
        workload_id="control-deployment",
        name=_CONTROL_NAME,
        labels=_CONTROL_LABELS,
        args=["control"],
        service=CONTROL_SERVICE,
        replicas=1,
        strategy="Recreate",
        description="Owns the rotating Claude OAuth credential, pairing flow, polling cycle, and live API streams.",
        env=[
            DATABASE.app_secret.key("uri").env_var(env_name(ControlSettings, "database_url")),
            k8s.EnvVar(name=env_name(ControlSettings, "credentials_file"), value=_CREDENTIALS_FILE),
            k8s.EnvVar(name=env_name(ControlSettings, "port"), value=str(CONTROL_SERVICE.pod_port)),
        ],
        volumes=[
            k8s.Volume(
                name="data", persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=_DATA_CLAIM)
            )
        ],
        volume_mounts=[k8s.VolumeMount(name="data", mount_path=_DATA_DIR)],
        tolerate_control_plane=True,
    )


def _service_and_route(chart: Chart) -> None:
    k8s.KubeService(
        chart,
        "web-service",
        metadata=k8s.ObjectMeta(name=WEB_SERVICE.name, namespace=NAMESPACE),
        spec=k8s.ServiceSpec(selector=_WEB_LABELS, ports=[WEB_SERVICE.port.k8s_service_port()], type="ClusterIP"),
    )
    k8s.KubeService(
        chart,
        "control-service",
        metadata=k8s.ObjectMeta(name=CONTROL_SERVICE.name, namespace=NAMESPACE),
        spec=k8s.ServiceSpec(
            selector=_CONTROL_LABELS, ports=[CONTROL_SERVICE.port.k8s_service_port()], type="ClusterIP"
        ),
    )
    https_route(
        chart,
        "route",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        hostnames=[HOSTNAME],
        backend=WEB_SERVICE,
        hsts=False,
        listener=None,
    )


def _network_policies(chart: Chart) -> None:
    NetworkPolicy(
        chart,
        "web-network-policy",
        metadata=ApiObjectMetadata(
            name=f"{NAME}-web",
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "The Gateway may reach the page. Web Pods may reach Authentik, the database, and the private "
                    "control Service."
                )
            },
        ),
        endpoint_selector=_WEB_ENDPOINT_LABELS,
        ingress=[IngressRule.from_gateway(WEB_SERVICE.pod_port)],
        egress=[
            cilium.dns_egress(resolves=["*"]),
            # auth.allegedly.works resolves to the Gateway's node addresses, which an FQDN rule cannot select.
            cilium.egress_via_gateway("auth.allegedly.works"),
            EgressRule.to_endpoints({"k8s:cnpg.io/cluster": DATABASE.name}, cnpg.PORT.number),
            CONTROL_SERVICE.egress(),
        ],
    )
    NetworkPolicy(
        chart,
        "control-network-policy",
        metadata=ApiObjectMetadata(
            name=f"{NAME}-control",
            namespace=NAMESPACE,
            annotations={
                "description": "Only web Pods may call the single Claude credential and sync control process."
            },
        ),
        endpoint_selector=_CONTROL_ENDPOINT_LABELS,
        ingress=[WEB_SERVICE.pods.admit(CONTROL_SERVICE.pod_port)],
        egress=[
            cilium.dns_egress(resolves=["*"]),
            EgressRule.to_fqdns(*_ANTHROPIC_HOSTS),
            EgressRule.to_endpoints({"k8s:cnpg.io/cluster": DATABASE.name}, cnpg.PORT.number),
        ],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=NAMESPACE,
        # An eviction to resize the pod would interrupt a token refresh.
        vpa=Vpa.DISABLED,
        agent_readable=None,
        labels={"name": NAMESPACE},
    )
    forgejo_images.forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAMESPACE)
    _database(chart)
    _data_claim(chart)
    _web_deployment(chart)
    _control_deployment(chart)
    _service_and_route(chart)
    _network_policies(chart)
    add_fleet_rules(chart)
    return chart


def claude_session_sync(
    flux_chart: Chart,
    directory: RenderedDirectory,
    cnpg_operator: Kustomization,
    external_secrets_operator: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        NAME,
        directory,
        description="Mirror of every Claude Code cloud session's events into a CNPG database, and its pairing page.",
        timeout="10m",
        # Owns the database and the credential volume.
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(cnpg_operator, external_secrets_operator),
    )
