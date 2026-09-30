"""claude-session-sync: the single Deployment that keeps a CNPG database level with every Claude
Code cloud session's events (`devinfra/claude/session_export`), its Namespace, the database, the
PVC holding the OAuth credential, the pull credentials and the egress policy.

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
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, NetworkPolicy
from devinfra.claude.session_export.export_sessions import SyncSettings
from util.settings_contract import env_name

NAME = "claude-session-sync"
NAMESPACE = "claude-session-sync"
OUTPUT_DIR = f"{GENERATED_ROOT}/claude-session-sync"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/claude-session-sync-image-pins"
DATABASE = cnpg.PostgresRef.generated(name="claude-session-sync-db", namespace=NAMESPACE)
# The tag comes from the Component in PINS_DIR.
_IMAGE = "git.allegedly.works/ducktape-ci/claude-session-sync:unset"
_LABELS = {"app.kubernetes.io/name": NAME}
_DATA_CLAIM = "claude-session-sync-data"
_DATA_DIR = "/data"
_CREDENTIALS_FILE = f"{_DATA_DIR}/credentials.json"
# The hosts the OAuth token is refreshed at and the sessions API is read from.
_ANTHROPIC_HOSTS = ("api.anthropic.com", "platform.claude.com")
_UID = 1000


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


def _deployment(chart: Chart) -> None:
    deployment = k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=NAMESPACE,
            labels=_LABELS,
            annotations={
                "description": (
                    "Reads every Claude Code cloud session's events into the CNPG database with a dedicated OAuth "
                    "grant kept on the data volume. Waits for `pair` to be run in the container."
                )
            },
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            selector=k8s.LabelSelector(match_labels=_LABELS),
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    image_pull_secrets=[k8s.LocalObjectReference(name=forgejo_images.SECRET_NAME)],
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True, run_as_user=_UID, run_as_group=_UID, fs_group=_UID
                    ),
                    containers=[
                        k8s.Container(
                            name=NAME,
                            image=_IMAGE,
                            args=["sync", "--credentials-file", _CREDENTIALS_FILE],
                            env=[DATABASE.app_secret.key("uri").env_var(env_name(SyncSettings, "database_url"))],
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
                            volume_mounts=[k8s.VolumeMount(name="data", mount_path=_DATA_DIR)],
                        )
                    ],
                    volumes=[
                        k8s.Volume(
                            name="data",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=_DATA_CLAIM),
                        )
                    ],
                ),
            ),
        ),
    )
    pod_policy.harden(deployment)
    # The descheduler evicts a pod on a contended worker roughly every 15 minutes
    # (cluster/cdk8s/cli_proxy_api/cli_proxy_api.py); this pod owns a rotating refresh token, so the
    # zone's control planes are allowed as overflow capacity.
    pod_policy.place(deployment, node_scheduling.HIL_OVH, tolerate_control_plane=True)


def _network_policy(chart: Chart) -> None:
    NetworkPolicy(
        chart,
        "egress",
        metadata=ApiObjectMetadata(
            name=NAME,
            namespace=NAMESPACE,
            annotations={
                "description": "The sync may resolve DNS, reach Anthropic's OAuth and API hosts, and use its database."
            },
        ),
        endpoint_selector=_LABELS,
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
    _deployment(chart)
    _network_policy(chart)
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
        description="Mirror of every Claude Code cloud session's events into a CNPG database.",
        timeout="10m",
        # Owns the database and the credential volume.
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(cnpg_operator, external_secrets_operator),
    )
