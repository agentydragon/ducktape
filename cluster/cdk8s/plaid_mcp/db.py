"""plaid-mcp's `db/`: the CNPG Postgres mirror of the Plaid data, its ESO-minted read-only
`plaid_ro` credentials, and the one-shot Job granting that role read access.

Hand-written beside the generated output: `readonly-role.sql`, which the generated
`kustomization.yaml`'s configMapGenerator hash-suffixes, so an edit re-creates the Job
(its `force` annotation).
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ServiceAccount, k8s
from cnpg_cluster_crds.io.cnpg.postgresql import (
    ClusterSpecManaged,
    ClusterSpecManagedRoles,
    ClusterSpecManagedRolesEnsure,
    ClusterSpecManagedRolesPasswordSecret,
)
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)

from cluster.cdk8s import cilium, cnpg, node_scheduling
from cluster.cdk8s.external_secrets.minted_secret import mint_db_role_secret
from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.flux import ConfigMapArgs, kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, NetworkPolicy
from cluster.cdk8s.providers.external_secrets.external_secret import DataFrom, ExternalSecret, SecretStoreRef
from cluster.cdk8s.secret_ref import SecretRef

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/plaid-mcp/db"
NAMESPACE = "plaid-mcp"
POSTGRES = cnpg.PostgresRef.generated(name="plaid-mcp-db", namespace=NAMESPACE)
_DATABASE = "plaidmcp"
_READONLY_ROLE = "plaid_ro"
READONLY = SecretRef(namespace=NAMESPACE, name="plaid-mcp-db-readonly")
# The namespace holding a copy of the read-only credentials, for Haku's ad-hoc queries, and the
# identity that copy is read with.
_READONLY_CONSUMER = "haku-sandbox"
_READONLY_READER = "plaid-mcp-db-readonly-reader"
_PROVISIONER = "plaid-mcp-db-readonly-provisioner"
_PROVISIONER_LABELS = {"app": _PROVISIONER}
_SQL_CONFIG_MAP = "plaid-mcp-db-readonly-sql"
_SQL_FILE = "readonly-role.sql"


def _cluster(chart: Chart) -> None:
    cnpg.cluster(
        chart,
        "cluster",
        ref=POSTGRES,
        annotations={
            "description": (
                "CNPG Postgres mirror for Plaid link metadata, webhook queue state, and Plaid-shaped financial data."
            )
        },
        placement=node_scheduling.HIL_OVH,
        storage_class="local-path-ovh",
        size="5Gi",
        managed=ClusterSpecManaged(
            roles=[
                ClusterSpecManagedRoles(
                    name=_READONLY_ROLE,
                    ensure=ClusterSpecManagedRolesEnsure.PRESENT,
                    login=True,
                    password_secret=ClusterSpecManagedRolesPasswordSecret(name=READONLY.name),
                    comment=(
                        "Read-only SQL access to the Plaid mirror; ESO copies the secret into the"
                        " haku-sandbox namespace."
                    ),
                )
            ]
        ),
        initdb=cnpg.same_owner_initdb(_DATABASE),
        wal_archive=False,
    )


def _readonly_credentials(chart: Chart) -> None:
    """A stable read-only password: ESO generates it once, with symbols disabled so the
    templated DATABASE_URL is safe without URL escaping."""
    mint_db_role_secret(
        chart,
        "readonly-external-secret",
        name=READONLY.name,
        namespace=NAMESPACE,
        role=_READONLY_ROLE,
        host=POSTGRES.rw.host,
        port=POSTGRES.rw.port.number,
        database=_DATABASE,
        secret_type=None,
    )


def _readonly_copy(chart: Chart) -> None:
    """The consumer namespace's copy of the read-only credentials, read through a store that
    reaches only that one Secret here. ESO polls the source, so a new password reaches the copy
    within the refresh interval."""
    reader = ServiceAccount(
        chart,
        "consumer-reader",
        metadata=ApiObjectMetadata(name=_READONLY_READER, namespace=_READONLY_CONSUMER),
        automount_token=False,
    )
    store = single_secret_store(
        chart,
        f"{_READONLY_CONSUMER}-{READONLY.name}",
        reader=reader,
        source_namespace=NAMESPACE,
        source_secret=READONLY.name,
        consumer_namespace=_READONLY_CONSUMER,
    )
    ExternalSecret(
        chart,
        "consumer-copy",
        metadata=ApiObjectMetadata(name=READONLY.name, namespace=_READONLY_CONSUMER),
        refresh_interval="10m",
        secret_store_ref=SecretStoreRef.cluster(store),
        data_from=[DataFrom.from_extract(READONLY.name)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
    )


def _readonly_provisioner(chart: Chart) -> None:
    NetworkPolicy(
        chart,
        "provisioner-egress",
        metadata=ApiObjectMetadata(
            name="plaid-mcp-db-readonly-provisioner-egress",
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Allow the one-shot readonly-role provisioner to resolve and connect to the Plaid CNPG primary."
                )
            },
        ),
        endpoint_selector=_PROVISIONER_LABELS,
        # Cilium exposes Kubernetes pod labels with the k8s: prefix.
        egress=[cilium.dns_egress(), EgressRule.to_endpoints({"k8s:cnpg.io/cluster": POSTGRES.name}, 5432)],
    )
    k8s.KubeJob(
        chart,
        "provisioner",
        metadata=k8s.ObjectMeta(
            name=_PROVISIONER,
            namespace=NAMESPACE,
            annotations={
                "description": "Applies read-only object GRANTs for plaid_ro after the CNPG cluster creates the role.",
                "kustomize.toolkit.fluxcd.io/force": "enabled",
            },
        ),
        spec=k8s.JobSpec(
            # No `ttlSecondsAfterFinished`, for the same reason as the study-casino twin:
            # a TTL would turn this change-driven GRANT script into an on-schedule one.
            # The cost is that a failed run holds this Kustomization unready until someone
            # deletes the Job -- see cluster/docs/troubleshooting.md, "A failed Job wedges
            # its Flux Kustomization". Re-run by editing readonly-role.sql, which re-hashes
            # the ConfigMap and lets the `force` annotation recreate the Job.
            backoff_limit=0,
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_PROVISIONER_LABELS),
                spec=k8s.PodSpec(
                    restart_policy="Never",
                    node_selector=node_scheduling.HIL_OVH_NODE_SELECTOR,
                    containers=[
                        k8s.Container(
                            name="psql",
                            image="ghcr.io/cloudnative-pg/postgresql:18.6-system-trixie",
                            command=["/bin/bash", "-c"],
                            args=[f"set -x\nexec psql \\\n  --set=ON_ERROR_STOP=1 \\\n  -f /sql/{_SQL_FILE}\n"],
                            termination_message_policy="FallbackToLogsOnError",
                            env=[
                                POSTGRES.app_secret.key("username").env_var("PGUSER"),
                                POSTGRES.app_secret.key("password").env_var("PGPASSWORD"),
                                k8s.EnvVar(name="PGHOST", value=POSTGRES.rw.host),
                                k8s.EnvVar(name="PGDATABASE", value=_DATABASE),
                            ],
                            volume_mounts=[k8s.VolumeMount(name="sql", mount_path="/sql", read_only=True)],
                        )
                    ],
                    volumes=[k8s.Volume(name="sql", config_map=k8s.ConfigMapVolumeSource(name=_SQL_CONFIG_MAP))],
                ),
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, POSTGRES.name, disable_resource_name_hashes=True)
    _readonly_credentials(chart)
    _readonly_copy(chart)
    _cluster(chart)
    _readonly_provisioner(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(
            resources=[write_charts(root, OUTPUT_DIR, chart)],
            config_map_generator=[ConfigMapArgs(name=_SQL_CONFIG_MAP, namespace=NAMESPACE, files=[_SQL_FILE])],
        ),
    )
