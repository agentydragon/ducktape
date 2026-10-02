"""Shared CNPG storage for the MCP servers' Authentik OAuth registrations and tokens.

Each OAuth facade gets a database, owner role, password Secret and namespace-scoped
ClusterSecretStore. Consumer Kustomizations copy only their own DSN into their namespace.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import Role, RoleBinding, RolePolicyRule, Secret, ServiceAccount
from cnpg_cluster_crds.io.cnpg.postgresql import (
    ClusterSpecManaged,
    ClusterSpecManagedRoles,
    ClusterSpecManagedRolesEnsure,
    ClusterSpecManagedRolesPasswordSecret,
)
from cnpg_database_crds.io.cnpg.postgresql import (
    DatabaseSpecCluster,
    DatabaseSpecDatabaseReclaimPolicy,
    DatabaseSpecEnsure,
)
from external_secret_store_crds.io.external_secrets import ClusterSecretStoreSpecProviderKubernetesAuthServiceAccount
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
)
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecDeletionPolicy,
    KustomizationSpecHealthCheckExprs,
)
from source_watcher_crds.io.fluxcd.extensions.source import ArtifactGeneratorSpecArtifacts

from cluster.cdk8s import cnpg, namespaces, node_scheduling
from cluster.cdk8s.external_secrets.kubernetes_store import cluster_secret_store
from cluster.cdk8s.external_secrets.minted_secret import mint_db_role_secret
from cluster.cdk8s.flux import (
    Kustomization,
    flux_kustomization,
    flux_kustomization_depends_on_many,
    kustomize_kustomization,
)
from cluster.cdk8s.generation import CNPG_DATABASE_READY, write_charts, write_yaml
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.cnpg.database import Database
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, SecretStoreRef, remote_data

OUTPUT_DIR = f"{GENERATED_ROOT}/mcp-oauth-state"
NAMESPACE = "mcp-oauth-state"
DATABASE = cnpg.PostgresRef.generated(name="mcp-oauth-state-db", namespace=NAMESPACE)
CONSUMER_SECRET = "mcp-oauth-db-credentials"


@dataclass(frozen=True)
class OAuthStateStore:
    """Names that identify one OAuth-state database and its sole consumer."""

    id: str
    namespace: str
    database: str
    role: str

    @property
    def source_secret(self) -> str:
        return f"mcp-oauth-{self.id}-credentials"

    @property
    def secret_store(self) -> str:
        return f"mcp-oauth-{self.id}-secret-store"


GROCY_SF = OAuthStateStore(
    id="grocy-sf", namespace="grocy-sf", database="mcp_oauth_grocy_sf", role="mcp_oauth_grocy_sf"
)
GROCY_VALLEJO = OAuthStateStore(
    id="grocy-vallejo", namespace="grocy-vallejo", database="mcp_oauth_grocy_vallejo", role="mcp_oauth_grocy_vallejo"
)
TANA = OAuthStateStore(id="tana", namespace="tana-mcp", database="mcp_oauth_tana", role="mcp_oauth_tana")
STORES = (GROCY_SF, GROCY_VALLEJO, TANA)
# PR #8568 removes the Plaid MCP and its OAuth store from normal management. Its Database CR used
# databaseReclaimPolicy=retain, so these tombstones explicitly drop the retained database and its
# owner role. The database must be gone before CNPG can drop the role.
_RETIRED_PLAID_DB = OAuthStateStore(
    id="plaid-db", namespace="plaid-mcp", database="mcp_oauth_plaid_db", role="mcp_oauth_plaid_db"
)


def _namespace_chart(app: App) -> Chart:
    chart = Chart(app, "namespace", disable_resource_name_hashes=True)
    namespaces.namespace(
        chart,
        "namespace",
        name=NAMESPACE,
        vpa=Vpa.RECOMMEND,
        # This namespace contains OAuth client registrations and bearer/refresh tokens.
    )
    return chart


def _consumer_secret_access(chart: Chart, store: OAuthStateStore) -> None:
    """Let ESO read exactly this store's source Secret, only for its consumer namespace."""
    reader = ServiceAccount(
        chart,
        f"{store.id}-secret-reader",
        metadata=ApiObjectMetadata(name=f"mcp-oauth-{store.id}-reader", namespace=NAMESPACE),
        automount_token=False,
    )
    role = Role(
        chart,
        f"{store.id}-secret-reader-role",
        metadata=ApiObjectMetadata(name=f"mcp-oauth-{store.id}-reader", namespace=NAMESPACE),
        rules=[
            RolePolicyRule(
                resources=[Secret.from_secret_name(chart, f"{store.id}-credentials-resource", store.source_secret)],
                verbs=["get"],
            )
        ],
    )
    RoleBinding(
        chart,
        f"{store.id}-secret-reader-binding",
        metadata=ApiObjectMetadata(name=f"mcp-oauth-{store.id}-reader", namespace=NAMESPACE),
        role=role,
    ).add_subjects(reader)
    cluster_secret_store(
        chart,
        f"{store.id}-cluster-secret-store",
        metadata=ApiObjectMetadata(
            name=store.secret_store,
            annotations={
                "description": (
                    f"Allows only the {store.namespace} MCP OAuth consumer to read its DSN from {NAMESPACE}."
                )
            },
        ),
        namespaces=[store.namespace],
        remote_namespace=NAMESPACE,
        service_account=ClusterSecretStoreSpecProviderKubernetesAuthServiceAccount(
            name=reader.name, namespace=NAMESPACE
        ),
    )


def _db_chart(app: App) -> Chart:
    chart = Chart(app, DATABASE.name, disable_resource_name_hashes=True)
    for store in STORES:
        mint_db_role_secret(
            chart,
            f"{store.id}-credentials",
            name=store.source_secret,
            namespace=NAMESPACE,
            role=store.role,
            host=DATABASE.rw.host,
            port=DATABASE.rw.port.number,
            database=store.database,
            url_key="uri",
        )

    cnpg.cluster(
        chart,
        "cluster",
        ref=DATABASE,
        annotations={"description": "Shared OVH-HA CNPG cluster for MCP OAuth state."},
        placement=node_scheduling.HIL_OVH,
        storage_class="local-path-ovh-hdd",
        size="5Gi",
        initdb=cnpg.same_owner_initdb("app"),
        wal_archive=False,
        managed=ClusterSpecManaged(
            roles=[
                ClusterSpecManagedRoles(
                    name=store.role,
                    ensure=ClusterSpecManagedRolesEnsure.PRESENT,
                    login=True,
                    password_secret=ClusterSpecManagedRolesPasswordSecret(name=store.source_secret),
                )
                for store in STORES
            ]
            + [ClusterSpecManagedRoles(name=_RETIRED_PLAID_DB.role, ensure=ClusterSpecManagedRolesEnsure.ABSENT)]
        ),
    )

    for store in STORES:
        Database(
            chart,
            f"database-{store.id}",
            # CNPG's Database CR metadata.name is a Kubernetes DNS name; the
            # PostgreSQL database name in spec may still contain underscores.
            metadata=ApiObjectMetadata(name=f"mcp-oauth-{store.id}-database", namespace=NAMESPACE),
            cluster=DatabaseSpecCluster(name=DATABASE.name),
            name=store.database,
            owner=store.role,
            # OAuth associations may be recreated after a deliberate reset, but pruning this
            # Database CR should not silently remove its backing database.
            database_reclaim_policy=DatabaseSpecDatabaseReclaimPolicy.RETAIN,
        )
        _consumer_secret_access(chart, store)

    Database(
        chart,
        "database-plaid-db-tombstone",
        metadata=ApiObjectMetadata(name=f"mcp-oauth-{_RETIRED_PLAID_DB.id}-database", namespace=NAMESPACE),
        cluster=DatabaseSpecCluster(name=DATABASE.name),
        name=_RETIRED_PLAID_DB.database,
        owner=_RETIRED_PLAID_DB.role,
        ensure=DatabaseSpecEnsure.ABSENT,
    )
    return chart


def add_consumer_credentials(scope: Chart, store: OAuthStateStore) -> None:
    """Copy one store's DSN into its consumer namespace with the narrow ClusterSecretStore."""
    ExternalSecret(
        scope,
        f"{store.id}-oauth-db-credentials",
        metadata=ApiObjectMetadata(
            name=CONSUMER_SECRET,
            namespace=store.namespace,
            annotations={"description": f"PostgreSQL DSN for the {store.namespace} MCP OAuth state store."},
        ),
        refresh_interval="10m",
        secret_store_ref=SecretStoreRef.cluster(store.secret_store),
        data=[remote_data(store.source_secret, "uri")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.DELETE,
    )


def write_manifests(root: Path) -> None:
    write_yaml(
        root / OUTPUT_DIR / "db" / "kustomization.yaml",
        kustomize_kustomization(resources=[write_charts(root, f"{OUTPUT_DIR}/db", _db_chart)]),
    )
    write_yaml(
        root / OUTPUT_DIR / "kustomization.yaml",
        kustomize_kustomization(resources=[write_charts(root, OUTPUT_DIR, _namespace_chart), "db"]),
    )


def mcp_oauth_state_db(
    chart: Chart,
    artifact: ArtifactGeneratorSpecArtifacts,
    cnpg_operator: Kustomization,
    external_secrets_operator: Kustomization,
    monitoring_crds: Kustomization,
    kyverno: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        "mcp-oauth-state-db",
        artifact,
        timeout="10m",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        health_check_exprs=[
            KustomizationSpecHealthCheckExprs(
                api_version="postgresql.cnpg.io/v1", kind="Database", current=CNPG_DATABASE_READY
            )
        ],
        depends_on=flux_kustomization_depends_on_many(
            cnpg_operator, external_secrets_operator, monitoring_crds, kyverno
        ),
        description="Shared OVH-HA CNPG cluster and per-MCP databases for OAuth client state.",
    )
