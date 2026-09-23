"""The console's CNPG Postgres: the distributed approval ledger for MCP tool calls, the
`vector` extension the semantic index needs, and the ESO-generated credential of the
narrow `haku_indexer` role.

This shares the `haku-console` Kustomization with the migration Job and the console
workloads that read the `<cluster>-app` Secret CNPG mints, so nothing orders them behind
it; they retry until the Cluster accepts connections.
"""

from __future__ import annotations

from cnpg_cluster_crds.io.cnpg.postgresql import (
    ClusterSpecBootstrapInitdb,
    ClusterSpecManaged,
    ClusterSpecManagedRoles,
    ClusterSpecManagedRolesEnsure,
    ClusterSpecManagedRolesPasswordSecret,
)
from cnpg_database_crds.io.cnpg.postgresql import (
    Database,
    DatabaseSpec,
    DatabaseSpecCluster,
    DatabaseSpecDatabaseReclaimPolicy,
    DatabaseSpecExtensions,
    DatabaseSpecExtensionsEnsure,
)
from constructs import Construct
from eso_password_generator_crds.io.external_secrets.generators import Password, PasswordSpec
from external_secrets_crds.io.external_secrets import (
    ExternalSecret,
    ExternalSecretSpec,
    ExternalSecretSpecDataFrom,
    ExternalSecretSpecDataFromSourceRef,
    ExternalSecretSpecDataFromSourceRefGeneratorRef,
    ExternalSecretSpecDataFromSourceRefGeneratorRefKind,
    ExternalSecretSpecTarget,
    ExternalSecretSpecTargetTemplate,
)

from cluster.cdk8s import cnpg
from cluster.cdk8s.agentplane import node_scheduling
from cluster.cdk8s.metadata import metadata

NAMESPACE = "haku-console"
CLUSTER_NAME = "haku-console-db"
DATABASE = "approval_store"
# CNPG mints the initdb owner's credentials into this Secret.
APP_SECRET = f"{CLUSTER_NAME}-app"
INDEXER_ROLE = "haku_indexer"
INDEXER_SECRET = f"{CLUSTER_NAME}-indexer"
RW_HOST = f"{CLUSTER_NAME}-rw.{NAMESPACE}.svc"
_POSTGRES_PORT = 5432


class Db(Construct):
    """The Cluster, the `approval_store` Database it adopts, and the indexer role's credential."""

    def __init__(self, scope: Construct, id: str) -> None:
        super().__init__(scope, id)
        self._add_indexer_credential()
        cnpg.cluster(
            self,
            "cluster",
            name=CLUSTER_NAME,
            namespace=NAMESPACE,
            # CNPG owns the data PVCs through this object, and nothing backs this database
            # up, so a prune is unrecoverable. The annotation exempts it whichever
            # Kustomization's inventory lists it (cluster/cdk8s/AGENTS.md) -- including
            # while ownership moves between them. Removing this Cluster is a deliberate
            # `kubectl delete`, never a manifest edit.
            annotations={"kustomize.toolkit.fluxcd.io/prune": "disabled"},
            affinity=cnpg.affinity(
                node_selector={"topology.kubernetes.io/zone": node_scheduling.ZONE}, tolerate_control_plane=False
            ),
            storage_class="local-path-ovh",
            size="2Gi",
            initdb=ClusterSpecBootstrapInitdb(database=DATABASE, owner=DATABASE),
            managed=ClusterSpecManaged(
                roles=[
                    # The haku-indexer worker's narrow credential: recall-index read/write.
                    # CNPG owns role existence and password sync; the object GRANTs are
                    # applied by the console Kustomization's indexer-role provisioner Job,
                    # since they target the recall_index schema the migration Job creates.
                    ClusterSpecManagedRoles(
                        name=INDEXER_ROLE,
                        ensure=ClusterSpecManagedRolesEnsure.PRESENT,
                        login=True,
                        password_secret=ClusterSpecManagedRolesPasswordSecret(name=INDEXER_SECRET),
                        comment=(
                            "haku-indexer worker: recall_index schema read/write "
                            "(see cluster/k8s/haku/console/indexer-role.sql)"
                        ),
                    )
                ]
            ),
        )
        # Declares the `vector` extension for the semantic index (`haku/recall_index`), whose
        # Alembic migration creates `vector` columns. pgvector is not a trusted extension, so
        # `CREATE EXTENSION` needs the superuser connection CNPG's reconciler holds, while the
        # migration runs as `approval_store`. Adopts the database `bootstrap.initdb` created,
        # hence `retain`: with `delete`, dropping this object would drop the console's whole
        # database.
        Database(
            self,
            "database",
            metadata=metadata(f"{CLUSTER_NAME}-approval-store", NAMESPACE),
            spec=DatabaseSpec(
                cluster=DatabaseSpecCluster(name=CLUSTER_NAME),
                name=DATABASE,
                owner=DATABASE,
                database_reclaim_policy=DatabaseSpecDatabaseReclaimPolicy.RETAIN,
                extensions=[DatabaseSpecExtensions(name="vector", ensure=DatabaseSpecExtensionsEnsure.PRESENT)],
            ),
        )

    def _add_indexer_credential(self) -> None:
        """The indexer role's password, ESO-generated once (symbols disabled so the templated
        URL needs no escaping). Its object-level privileges are the narrow set the provisioner
        Job grants -- never the application owner's."""
        generator = f"{INDEXER_SECRET}-generator"
        Password(
            self,
            "indexer-password",
            metadata=metadata(generator, NAMESPACE),
            spec=PasswordSpec(length=40, digits=8, symbols=0, no_upper=False, allow_repeat=True),
        )
        ExternalSecret(
            self,
            "indexer-secret",
            metadata=metadata(INDEXER_SECRET, NAMESPACE),
            spec=ExternalSecretSpec(
                refresh_interval="8760h",
                target=ExternalSecretSpecTarget(
                    name=INDEXER_SECRET,
                    template=ExternalSecretSpecTargetTemplate(
                        type="kubernetes.io/basic-auth",
                        data={
                            "username": INDEXER_ROLE,
                            "password": "{{ .password }}",
                            # The SQLAlchemy asyncpg form the worker consumes directly.
                            "DATABASE_URL": (
                                f"postgresql+asyncpg://{INDEXER_ROLE}:{{{{ .password }}}}"
                                f"@{RW_HOST}:{_POSTGRES_PORT}/{DATABASE}"
                            ),
                        },
                    ),
                ),
                data_from=[
                    ExternalSecretSpecDataFrom(
                        source_ref=ExternalSecretSpecDataFromSourceRef(
                            generator_ref=ExternalSecretSpecDataFromSourceRefGeneratorRef(
                                kind=ExternalSecretSpecDataFromSourceRefGeneratorRefKind.PASSWORD, name=generator
                            )
                        )
                    )
                ],
            ),
        )
