"""plaid-mcp's `db/`: the CNPG Postgres mirror of the Plaid data, its ESO-minted read-only
`plaid_ro` credentials, and the one-shot Job granting that role read access.

Hand-written beside the generated output: `readonly-role.sql`, which the generated
`kustomization.yaml`'s configMapGenerator hash-suffixes, so an edit re-creates the Job
(its `force` annotation).
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import ServiceAccount, k8s
from cnpg_cluster_crds.io.cnpg.postgresql import (
    ClusterSpecBootstrapInitdb,
    ClusterSpecManaged,
    ClusterSpecManagedRoles,
    ClusterSpecManagedRolesEnsure,
    ClusterSpecManagedRolesPasswordSecret,
)
from eso_password_generator_crds.io.external_secrets.generators import Password, PasswordSpec
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
)

from cluster.cdk8s import cilium, cnpg
from cluster.cdk8s.external_secrets.single_secret_store import single_secret_store
from cluster.cdk8s.flux import ConfigMapArgs, kustomize_kustomization
from cluster.cdk8s.generation import write_charts, write_yaml
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.metadata import metadata
from cluster.cdk8s.plaid_mcp.app import NAMESPACE
from cluster.cdk8s.providers.external_secrets.external_secret import DataFrom, ExternalSecret, SecretStoreRef

OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/agents/plaid-mcp/db"
_CLUSTER = "plaid-mcp-db"
_DATABASE = "plaidmcp"
_READONLY_ROLE = "plaid_ro"
_READONLY_SECRET = "plaid-mcp-db-readonly"
_READONLY_GENERATOR = "plaid-mcp-db-readonly-generator"
# The namespace holding a copy of the read-only credentials, for Haku's ad-hoc queries, and the
# identity that copy is read with.
_READONLY_CONSUMER = "haku-sandbox"
_READONLY_READER = "plaid-mcp-db-readonly-reader"
_PROVISIONER = "plaid-mcp-db-readonly-provisioner"
_PROVISIONER_LABELS = {"app": _PROVISIONER}
_SQL_CONFIG_MAP = "plaid-mcp-db-readonly-sql"
_SQL_FILE = "readonly-role.sql"
_PRIMARY_HOST = f"{_CLUSTER}-rw.{NAMESPACE}.svc"
_ZONE = "hil-ovh"


def _cluster(chart: Chart) -> None:
    cnpg.cluster(
        chart,
        "cluster",
        name=_CLUSTER,
        namespace=NAMESPACE,
        annotations={
            "description": (
                "CNPG Postgres mirror for Plaid link metadata, full-refresh sync state, and Plaid-shaped"
                " financial data."
            )
        },
        node_selector={"topology.kubernetes.io/zone": _ZONE},
        storage_class="local-path-ovh",
        size="5Gi",
        managed=ClusterSpecManaged(
            roles=[
                ClusterSpecManagedRoles(
                    name=_READONLY_ROLE,
                    ensure=ClusterSpecManagedRolesEnsure.PRESENT,
                    login=True,
                    password_secret=ClusterSpecManagedRolesPasswordSecret(name=_READONLY_SECRET),
                    comment=(
                        "Read-only SQL access for the Plaid Postgres MCP facade; ESO copies the secret into the"
                        " haku-sandbox namespace."
                    ),
                )
            ]
        ),
        # CNPG auto-generates credentials in secret plaid-mcp-db-app.
        initdb=ClusterSpecBootstrapInitdb(database=_DATABASE, owner=_DATABASE),
    )


def _readonly_credentials(chart: Chart) -> None:
    """A stable read-only password: ESO generates it once, with symbols disabled so the
    templated DATABASE_URL is safe without URL escaping."""
    Password(
        chart,
        "readonly-password-generator",
        metadata=metadata(_READONLY_GENERATOR, NAMESPACE),
        spec=PasswordSpec(length=40, digits=8, symbols=0, no_upper=False, allow_repeat=True),
    )
    ExternalSecret(
        chart,
        "readonly-external-secret",
        name=_READONLY_SECRET,
        namespace=NAMESPACE,
        refresh="8760h",
        data_from=[DataFrom.from_password_generator(_READONLY_GENERATOR)],
        template=ExternalSecretSpecTargetTemplate(
            data={
                "username": _READONLY_ROLE,
                "password": "{{ .password }}",
                "host": _PRIMARY_HOST,
                "port": "5432",
                "dbname": _DATABASE,
                "DATABASE_URL": f"postgresql://{_READONLY_ROLE}:{{{{ .password }}}}@{_PRIMARY_HOST}:5432/{_DATABASE}",
            }
        ),
    )


def _readonly_copy(chart: Chart) -> None:
    """The consumer namespace's copy of the read-only credentials, read through a store that
    reaches only that one Secret here. ESO polls the source, so a new password reaches the copy
    within the refresh interval."""
    reader = ServiceAccount(
        chart, "consumer-reader", metadata=metadata(_READONLY_READER, _READONLY_CONSUMER), automount_token=False
    )
    store = single_secret_store(
        chart,
        f"{_READONLY_CONSUMER}-{_READONLY_SECRET}",
        reader=reader,
        source_namespace=NAMESPACE,
        source_secret=_READONLY_SECRET,
        consumer_namespace=_READONLY_CONSUMER,
    )
    ExternalSecret(
        chart,
        "consumer-copy",
        name=_READONLY_SECRET,
        namespace=_READONLY_CONSUMER,
        refresh="10m",
        store=SecretStoreRef.cluster(store),
        data_from=[DataFrom.from_extract(_READONLY_SECRET)],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
    )


def _app_secret_env(name: str, key: str) -> k8s.EnvVar:
    return k8s.EnvVar(
        name=name, value_from=k8s.EnvVarSource(secret_key_ref=k8s.SecretKeySelector(name=f"{_CLUSTER}-app", key=key))
    )


def _readonly_provisioner(chart: Chart) -> None:
    cilium.network_policy(
        chart,
        "provisioner-egress",
        metadata=metadata(
            "plaid-mcp-db-readonly-provisioner-egress",
            NAMESPACE,
            annotations={
                "description": (
                    "Allow the one-shot readonly-role provisioner to resolve and connect to the Plaid CNPG primary."
                )
            },
        ),
        selector=_PROVISIONER_LABELS,
        # Cilium exposes Kubernetes pod labels with the k8s: prefix.
        egress=[cilium.dns_egress(), cilium.egress_to({"k8s:cnpg.io/cluster": _CLUSTER}, 5432)],
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
                    node_selector={"topology.kubernetes.io/zone": _ZONE},
                    containers=[
                        k8s.Container(
                            name="psql",
                            image="ghcr.io/cloudnative-pg/postgresql:18.6-system-trixie",
                            command=["/bin/bash", "-c"],
                            args=[f"set -x\nexec psql \\\n  --set=ON_ERROR_STOP=1 \\\n  -f /sql/{_SQL_FILE}\n"],
                            termination_message_policy="FallbackToLogsOnError",
                            env=[
                                _app_secret_env("PGUSER", "username"),
                                _app_secret_env("PGPASSWORD", "password"),
                                k8s.EnvVar(name="PGHOST", value=_PRIMARY_HOST),
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
    chart = Chart(app, _CLUSTER, disable_resource_name_hashes=True)
    _readonly_credentials(chart)
    _readonly_copy(chart)
    _cluster(chart)
    _readonly_provisioner(chart)
    return chart


def write_manifests(root: Path) -> None:
    write_charts(root, OUTPUT_DIR, chart)
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(
            resources=[f"{_CLUSTER}.k8s.yaml"],
            config_map_generator=[ConfigMapArgs(name=_SQL_CONFIG_MAP, namespace=NAMESPACE, files=[_SQL_FILE])],
        ),
    )
