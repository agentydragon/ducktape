"""study-casino: Namespace, CNPG Postgres, the ESO-minted read-only role credentials and the
Job granting that role read access, Deployment, Service, route and the agents' secrets-reader
binding.

The Job's `readonly-role.sql` lives beside this module and is copied in for the kustomization's
`configMapGenerator`. The one hand-written file is the `PINS_DIR` Component, which the
kustomization includes across the roots: it pins the image tag and copies it into
`STUDY_CASINO_IMAGE_TAG`.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from cnpg_cluster_crds.io.cnpg.postgresql import (
    ClusterSpecManaged,
    ClusterSpecManagedRoles,
    ClusterSpecManagedRolesEnsure,
    ClusterSpecManagedRolesPasswordSecret,
)
from constructs import Construct
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from gateway_api_crds.io.k8s.networking.gateway import (
    HttpRouteSpecRules,
    HttpRouteSpecRulesBackendRefs,
    HttpRouteSpecRulesFiltersResponseHeaderModifierSet,
)

from cluster.cdk8s import cnpg, namespaces, node_scheduling
from cluster.cdk8s.external_secrets.minted_secret import mint_db_role_secret
from cluster.cdk8s.flux import (
    ConfigMapArgs,
    Kustomization,
    RenderedDirectory,
    flux_kustomization,
    flux_kustomization_depends_on_many,
)
from cluster.cdk8s.forgejo_images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.gateway import cluster_gateway_parent_ref
from cluster.cdk8s.generation import copy_source_file
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.providers.gateway_api.http_route import HttpRoute, RouteFilter, RouteMatch
from cluster.cdk8s.reflector import mirror_annotations
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef

OUTPUT_DIR = f"{GENERATED_ROOT}/study-casino"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/study-casino-image-pins"
_NAME = "study-casino"
_NAMESPACE = "study-casino"
_SERVICE = ServiceRef(
    name=_NAME,
    port=Port(name="http", number=8080),
    pods=Pods(namespace=_NAMESPACE, labels=(("app.kubernetes.io/name", _NAME),)),
)
POSTGRES = cnpg.PostgresRef.generated(name="study-casino-db", namespace=_NAMESPACE)
_OIDC = SecretRef(namespace=_NAMESPACE, name="study-casino-oidc")
_DATABASE = "studycasino"
_READONLY_ROLE = "study_casino_ro"
_READONLY = SecretRef(namespace=_NAMESPACE, name="study-casino-db-ro")
_SQL_CONFIG_MAP = "study-casino-db-readonly-sql"
_REGION = "hil"
_IMMUTABLE = "public, max-age=31536000, immutable"
# The PINS_DIR Component overrides the tag and copies it into STUDY_CASINO_IMAGE_TAG.
_PLACEHOLDER_TAG = "unset"

_PROVISIONER_SCRIPT = textwrap.dedent(
    """\
    set -x
    echo "PGUSER=${PGUSER:-<unset>}"
    echo "PGHOST=${PGHOST:-<unset>}"
    echo "PGDATABASE=${PGDATABASE:-<unset>}"
    echo "PGPASSWORD set: ${PGPASSWORD:+yes}"
    exec psql \\
      --set=ON_ERROR_STOP=1 \\
      -f /sql/readonly-role.sql
    """
)


def _namespace(scope: Construct) -> None:
    namespaces.namespace(
        scope,
        "namespace",
        name=_NAMESPACE,
        # Single-pod personal app; opt out of Goldilocks/VPA recommendations.
        vpa=Vpa.DISABLED,
        labels={"name": _NAMESPACE},
    )


def _database(scope: Construct) -> None:
    cnpg.cluster(
        scope,
        "database",
        ref=POSTGRES,
        annotations={"description": "CNPG Postgres for study-casino state."},
        # 3 instances, one per OVH SSD (control-plane) node. Tolerates 1-node loss without read-quorum or
        # primary-availability impact.
        instances=3,
        image_name=None,
        placement=node_scheduling.Placement(node_selector={"topology.kubernetes.io/region": _REGION}),
        storage_class="local-path-ovh-ssd",
        size="1Gi",
        # CNPG creates the read-only role on first reconcile and sets its password from
        # the `passwordSecret`. Object-level GRANTs (CONNECT/USAGE/SELECT + ALTER DEFAULT
        # PRIVILEGES) are applied by the provisioner Job running as the `studycasino`
        # owner — `managed.roles` only covers role attributes, not object permissions.
        managed=ClusterSpecManaged(
            roles=[
                ClusterSpecManagedRoles(
                    name=_READONLY_ROLE,
                    ensure=ClusterSpecManagedRolesEnsure.PRESENT,
                    login=True,
                    password_secret=ClusterSpecManagedRolesPasswordSecret(name=_READONLY.name),
                    comment=(
                        "Read-only access for sandbox agents; object GRANTs come from the"
                        " study-casino-db-readonly-provisioner Job."
                    ),
                )
            ]
        ),
        initdb=cnpg.same_owner_initdb(_DATABASE),
        wal_archive=False,
    )


def _readonly_credentials(scope: Construct) -> None:
    """ESO mints the read-only password and replaces it at `mint_db_role_secret`'s refresh
    interval. `cnpg.io/reload` makes the CNPG operator watch the Secret, so a new password
    reaches the role when ESO writes it, not at the operator's next unrelated reconcile.
    Reflector mirrors the Secret into claude-sandbox."""
    mint_db_role_secret(
        scope,
        "readonly-credentials",
        name=_READONLY.name,
        namespace=_NAMESPACE,
        role=_READONLY_ROLE,
        host=POSTGRES.rw.host,
        port=POSTGRES.rw.port.number,
        database=_DATABASE,
        target_labels={"cnpg.io/reload": "true"},
        target_annotations={
            "description": (
                "Read-only Postgres credentials for sandbox agents. CNPG sets the study_casino_ro password from"
                " this Secret; Reflector mirrors it into claude-sandbox."
            ),
            **mirror_annotations(["claude-sandbox"]),
        },
    )


def _readonly_provisioner(scope: Construct) -> None:
    k8s.KubeJob(
        scope,
        "readonly-provisioner",
        metadata=k8s.ObjectMeta(
            name="study-casino-db-readonly-provisioner",
            namespace=_NAMESPACE,
            annotations={
                "description": (
                    "Applies the read-only object GRANTs for study_casino_ro. The role itself is managed "
                    "declaratively by CNPG (Cluster.spec.managed.roles)."
                ),
                # Force enables Flux to recreate the Job on manifest changes (e.g. when
                # readonly-role.sql is edited and kustomize re-hashes the ConfigMap).
                # Without this, the Job is immutable after first apply.
                "kustomize.toolkit.fluxcd.io/force": "enabled",
            },
        ),
        spec=k8s.JobSpec(
            # Single attempt — a failed pod is preserved so logs survive long enough
            # to debug. Re-run by editing readonly-role.sql (kustomize bumps the
            # ConfigMap hash; `kustomize.toolkit.fluxcd.io/force: enabled` then
            # deletes+recreates the Job, since Job specs are otherwise immutable).
            #
            # No `ttlSecondsAfterFinished`: if the TTL controller deleted a
            # successful Job, Flux would re-create it on the next reconcile,
            # turning a change-driven script into an on-schedule one.
            backoff_limit=0,
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels={"app": "study-casino-db-readonly-provisioner"}),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    restart_policy="Never",
                    node_selector={"topology.kubernetes.io/region": _REGION},
                    containers=[
                        k8s.Container(
                            name="psql",
                            image="ghcr.io/cloudnative-pg/postgresql:18.6",
                            command=["/bin/bash", "-c"],
                            args=[_PROVISIONER_SCRIPT],
                            termination_message_policy="FallbackToLogsOnError",
                            env=[
                                # Run as the database owner. Object-level GRANTs only need owner
                                # privileges, not superuser; this lets us drop enableSuperuserAccess.
                                POSTGRES.app_secret.key("username").env_var("PGUSER"),
                                POSTGRES.app_secret.key("password").env_var("PGPASSWORD"),
                                k8s.EnvVar(name="PGHOST", value=POSTGRES.rw.host),
                                k8s.EnvVar(name="PGDATABASE", value=_DATABASE),
                            ],
                            volume_mounts=[k8s.VolumeMount(name="sql", mount_path="/sql", read_only=True)],
                        )
                    ],
                    volumes=[
                        k8s.Volume(
                            name="sql",
                            # The kustomization's configMapGenerator (`config_maps`) adds the hash suffix.
                            config_map=k8s.ConfigMapVolumeSource(name=_SQL_CONFIG_MAP),
                        )
                    ],
                ),
            ),
        ),
    )


def _probe(initial_delay_seconds: int, period_seconds: int) -> k8s.Probe:
    return k8s.Probe(
        http_get=k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_number(_SERVICE.pod_port)),
        initial_delay_seconds=initial_delay_seconds,
        period_seconds=period_seconds,
    )


def _deployment(scope: Construct) -> None:
    k8s.KubeDeployment(
        scope,
        "deployment",
        metadata=k8s.ObjectMeta(
            name=_NAME,
            namespace=_NAMESPACE,
            labels=_SERVICE.pods.selector,
            annotations={
                "description": (
                    "Habit-tracking casino app. Serves PWA + /sync endpoint with OIDC auth (Authorization Code "
                    "flow, confidential client). State lives in CNPG Postgres (study-casino-db)."
                )
            },
        ),
        spec=k8s.DeploymentSpec(
            replicas=1,
            strategy=k8s.DeploymentStrategy(type="RollingUpdate"),
            selector=k8s.LabelSelector(match_labels=_SERVICE.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_SERVICE.pods.selector),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    node_selector={"topology.kubernetes.io/region": _REGION},
                    # Stateless: all state is in study-casino-db (CNPG), no local storage. Allow
                    # control-plane nodes as overflow capacity, but prefer workers to keep
                    # ordinary application I/O away from etcd disks.
                    tolerations=[node_scheduling.CONTROL_PLANE_TOLERATION],
                    affinity=node_scheduling.PREFER_WORKERS,
                    containers=[
                        k8s.Container(
                            name="app",
                            image=f"git.allegedly.works/ducktape-ci/study-casino:{_PLACEHOLDER_TAG}",
                            image_pull_policy="Always",
                            ports=[_SERVICE.port.k8s_container_port()],
                            env=[
                                k8s.EnvVar(name="STUDY_CASINO_IMAGE_TAG", value=_PLACEHOLDER_TAG),
                                # Compose the SQLAlchemy URL from the CNPG-generated secret. The
                                # `$(VAR)` syntax in env values is resolved by the kubelet from
                                # earlier-defined env vars in the same container. `+psycopg`
                                # forces SQLAlchemy to pick the psycopg v3 driver.
                                POSTGRES.app_secret.key("user").env_var("PG_USER"),
                                POSTGRES.app_secret.key("password").env_var("PG_PASSWORD"),
                                POSTGRES.app_secret.key("host").env_var("PG_HOST"),
                                POSTGRES.app_secret.key("port").env_var("PG_PORT"),
                                POSTGRES.app_secret.key("dbname").env_var("PG_DBNAME"),
                                k8s.EnvVar(
                                    name="STUDY_CASINO_DATABASE_URL",
                                    value="postgresql+psycopg://$(PG_USER):$(PG_PASSWORD)@$(PG_HOST):$(PG_PORT)/$(PG_DBNAME)",
                                ),
                                # Comma-separated. Admins can manage other users' prize catalogs
                                # via /admin/*; non-admins can redeem but not create or delete.
                                k8s.EnvVar(name="STUDY_CASINO_ADMIN_USERS", value="agentydragon"),
                                k8s.EnvVar(
                                    name="STUDY_CASINO_OIDC_ISSUER",
                                    value="https://auth.allegedly.works/application/o/study-casino/",
                                ),
                                k8s.EnvVar(name="STUDY_CASINO_OIDC_CLIENT_ID", value="study-casino"),
                                _OIDC.key("oidc_client_secret").env_var("STUDY_CASINO_OIDC_CLIENT_SECRET"),
                                _OIDC.key("session_secret").env_var("STUDY_CASINO_SESSION_SECRET"),
                                _OIDC.key("rng_secret").env_var("STUDY_CASINO_RNG_SECRET"),
                                k8s.EnvVar(name="STUDY_CASINO_RNG_KEY_ID", value="study-casino-rng-v1"),
                            ],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "memory": k8s.Quantity.from_string("64Mi"),
                                    "cpu": k8s.Quantity.from_string("25m"),
                                },
                                limits={"memory": k8s.Quantity.from_string("256Mi")},
                            ),
                            readiness_probe=_probe(3, 10),
                            liveness_probe=_probe(15, 20),
                        )
                    ],
                ),
            ),
        ),
    )


def _cache_rule(prefix: str, cache_control: str) -> HttpRouteSpecRules:
    return HttpRouteSpecRules(
        matches=[RouteMatch.path_prefix(prefix)],
        filters=[
            RouteFilter.response_header_modifier(
                set=[HttpRouteSpecRulesFiltersResponseHeaderModifierSet(name="Cache-Control", value=cache_control)]
            )
        ],
        backend_refs=[HttpRouteSpecRulesBackendRefs(name=_SERVICE.name, port=_SERVICE.port.number)],
    )


def _route(scope: Construct) -> None:
    # Three independent rules, not one https_route() call: each prefix needs its own
    # Cache-Control value, and https_route()'s one-rule-per-route shape has a single
    # shared filter list.
    HttpRoute(
        scope,
        "route",
        metadata=ApiObjectMetadata(name=_NAME, namespace=_NAMESPACE),
        parent_refs=[cluster_gateway_parent_ref()],
        hostnames=["casino.allegedly.works"],
        rules=[
            # Hermetic font files — content-stable; cache forever.
            _cache_rule("/fonts/", _IMMUTABLE),
            # Vite's content-hashed bundle output (after frontend migrates to Vite):
            # /assets/<name>-<hash>.{js,css,…}. URL changes whenever bytes change,
            # so safe to cache forever.
            _cache_rule("/assets/", _IMMUTABLE),
            # Everything else (index.html, sw.js, manifest, icon, current /main.js,
            # API endpoints): do not store. Bazel-normalized mtimes can make app-shell
            # validators lie across deploys, while hashed /assets/ remain immutable
            # above.
            _cache_rule("/", "no-store"),
        ],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    _namespace(chart)
    _database(chart)
    _readonly_credentials(chart)
    _readonly_provisioner(chart)
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=_NAMESPACE)
    _deployment(chart)
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=_SERVICE.name, namespace=_NAMESPACE),
        spec=k8s.ServiceSpec(selector=_SERVICE.pods.selector, ports=[_SERVICE.port.k8s_service_port()]),
    )
    _route(chart)
    k8s.KubeRoleBinding(
        chart,
        "agent-secrets-reader",
        metadata=k8s.ObjectMeta(name="agent-secrets-reader", namespace=_NAMESPACE),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="ClusterRole", name="secrets-reader"),
        subjects=[
            k8s.Subject(
                kind="Group", name="oidc-ksbx-groups:kubectl-sandbox-users", api_group="rbac.authorization.k8s.io"
            )
        ],
    )
    return chart


def config_maps(root: Path) -> list[ConfigMapArgs]:
    """Copy `readonly-role.sql` into `OUTPUT_DIR`; return the `configMapGenerator` entry."""
    return [
        ConfigMapArgs(
            name=_SQL_CONFIG_MAP,
            namespace=_NAMESPACE,
            files=[copy_source_file(root, OUTPUT_DIR, "cluster/cdk8s/study_casino/readonly-role.sql")],
        )
    ]


def study_casino(
    flux_chart: Chart,
    directory: RenderedDirectory,
    cnpg_operator: Kustomization,
    external_secrets_operator: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        _NAME,
        directory,
        suspend=False,
        timeout="10m",
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(cnpg_operator, external_secrets_operator),
    )
