"""study-casino: Namespace, CNPG Postgres, the read-only role provisioner Job, Deployment,
Service, route and the agents' secrets-reader binding.

Hand-written beside the generated output: `readonly-role.sql` (a `configMapGenerator` input),
the read-only role's SOPS Secret, the `kustomization.yaml` that generates the SQL ConfigMap, and
`image-pins/kustomization.yaml`, which pins the image tag and copies it into
`STUDY_CASINO_IMAGE_TAG`.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from cdk8s import App, Chart
from cdk8s_plus_34 import k8s
from cnpg_cluster_crds.io.cnpg.postgresql import (
    Cluster,
    ClusterSpec,
    ClusterSpecAffinity,
    ClusterSpecAffinityTolerations,
    ClusterSpecBootstrap,
    ClusterSpecBootstrapInitdb,
    ClusterSpecManaged,
    ClusterSpecManagedRoles,
    ClusterSpecManagedRolesEnsure,
    ClusterSpecManagedRolesPasswordSecret,
    ClusterSpecMonitoring,
    ClusterSpecProbes,
    ClusterSpecProbesLiveness,
    ClusterSpecProbesLivenessIsolationCheck,
    ClusterSpecStorage,
)
from constructs import Construct
from gateway_api_crds.io.k8s.networking.gateway import (
    HttpRoute,
    HttpRouteSpec,
    HttpRouteSpecRules,
    HttpRouteSpecRulesBackendRefs,
    HttpRouteSpecRulesFilters,
    HttpRouteSpecRulesFiltersResponseHeaderModifier,
    HttpRouteSpecRulesFiltersResponseHeaderModifierSet,
    HttpRouteSpecRulesFiltersType,
    HttpRouteSpecRulesMatches,
    HttpRouteSpecRulesMatchesPath,
    HttpRouteSpecRulesMatchesPathType,
)

from cluster.cdk8s.cnpg import OFF_CONTROL_PLANE_NODE_AFFINITY
from cluster.cdk8s.forgejo_images import SECRET_NAME, forgejo_images_creds_external_secret
from cluster.cdk8s.gateway import cluster_gateway_parent_ref
from cluster.cdk8s.generation import write_charts
from cluster.cdk8s.metadata import metadata

_OUTPUT_DIR = "cluster/k8s/study-casino"
_NAME = "study-casino"
_NAMESPACE = "study-casino"
_PORT = 8080
_DB_NAME = "study-casino-db"
_DATABASE = "studycasino"
_REGION = "hil"
_IMMUTABLE = "public, max-age=31536000, immutable"
# image-pins/ overrides the tag and copies it into STUDY_CASINO_IMAGE_TAG.
_PLACEHOLDER_TAG = "unset"
_LABELS = {"app.kubernetes.io/name": _NAME}

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
    k8s.KubeNamespace(
        scope,
        "namespace",
        metadata=k8s.ObjectMeta(
            name=_NAMESPACE,
            labels={
                "name": _NAMESPACE,
                # Single-pod personal app; opt out of Goldilocks/VPA recommendations.
                "goldilocks.fairwinds.com/enabled": "false",
                "rbac.ducktape.io/agent-readable-logs": "true",
            },
        ),
    )


def _database(scope: Construct) -> None:
    Cluster(
        scope,
        "database",
        metadata=metadata(_DB_NAME, _NAMESPACE, annotations={"description": "CNPG Postgres for study-casino state."}),
        spec=ClusterSpec(
            # 3 instances spread across 3 OVH nodes via the topologyKey=hostname
            # anti-affinity below. Tolerates 1-node loss without read-quorum or
            # primary-availability impact.
            instances=3,
            # CNPG 1.27+ kills isolated primaries by default (liveness probe).
            # Disable to prevent false positives from transient network blips.
            probes=ClusterSpecProbes(
                liveness=ClusterSpecProbesLiveness(
                    isolation_check=ClusterSpecProbesLivenessIsolationCheck(enabled=False)
                )
            ),
            affinity=ClusterSpecAffinity(
                node_selector={"topology.kubernetes.io/region": _REGION},
                tolerations=[
                    ClusterSpecAffinityTolerations(
                        key="node-role.kubernetes.io/control-plane", operator="Exists", effect="NoSchedule"
                    )
                ],
                # One PostgreSQL instance per HIL node for real HA.
                topology_key="kubernetes.io/hostname",
                node_affinity=OFF_CONTROL_PLANE_NODE_AFFINITY,
            ),
            storage=ClusterSpecStorage(storage_class="local-path-ovh", size="1Gi"),
            monitoring=ClusterSpecMonitoring(enable_pod_monitor=True),
            # Declaratively-managed roles. CNPG creates `study_casino_ro` on first
            # reconcile and keeps the password in sync with study-casino-db-readonly.
            # Object-level GRANTs (CONNECT/USAGE/SELECT + ALTER DEFAULT PRIVILEGES)
            # are applied by the provisioner Job running as the `studycasino` owner —
            # `managed.roles` only covers role attributes, not object permissions.
            managed=ClusterSpecManaged(
                roles=[
                    ClusterSpecManagedRoles(
                        name="study_casino_ro",
                        ensure=ClusterSpecManagedRolesEnsure.PRESENT,
                        login=True,
                        password_secret=ClusterSpecManagedRolesPasswordSecret(name="study-casino-db-readonly"),
                        comment=(
                            "Read-only access for sandbox agents (see cluster/k8s/study-casino/readonly-role.sql)"
                        ),
                    )
                ]
            ),
            # CNPG auto-generates credentials in secret study-casino-db-app
            bootstrap=ClusterSpecBootstrap(initdb=ClusterSpecBootstrapInitdb(database=_DATABASE, owner=_DATABASE)),
        ),
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
                                k8s.EnvVar(
                                    name="PGUSER",
                                    value_from=k8s.EnvVarSource(
                                        secret_key_ref=k8s.SecretKeySelector(name=f"{_DB_NAME}-app", key="username")
                                    ),
                                ),
                                k8s.EnvVar(
                                    name="PGPASSWORD",
                                    value_from=k8s.EnvVarSource(
                                        secret_key_ref=k8s.SecretKeySelector(name=f"{_DB_NAME}-app", key="password")
                                    ),
                                ),
                                k8s.EnvVar(name="PGHOST", value=f"{_DB_NAME}-rw.{_NAMESPACE}.svc"),
                                k8s.EnvVar(name="PGDATABASE", value=_DATABASE),
                            ],
                            volume_mounts=[k8s.VolumeMount(name="sql", mount_path="/sql", read_only=True)],
                        )
                    ],
                    volumes=[
                        k8s.Volume(
                            name="sql",
                            # Rendered by the hand-written kustomization.yaml's configMapGenerator.
                            config_map=k8s.ConfigMapVolumeSource(name="study-casino-db-readonly-sql"),
                        )
                    ],
                ),
            ),
        ),
    )


def _db_env(name: str, key: str) -> k8s.EnvVar:
    return k8s.EnvVar(
        name=name, value_from=k8s.EnvVarSource(secret_key_ref=k8s.SecretKeySelector(name=f"{_DB_NAME}-app", key=key))
    )


def _oidc_env(name: str, key: str) -> k8s.EnvVar:
    return k8s.EnvVar(
        name=name, value_from=k8s.EnvVarSource(secret_key_ref=k8s.SecretKeySelector(name="study-casino-oidc", key=key))
    )


def _probe(initial_delay_seconds: int, period_seconds: int) -> k8s.Probe:
    return k8s.Probe(
        http_get=k8s.HttpGetAction(path="/healthz", port=k8s.IntOrString.from_number(_PORT)),
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
            labels=_LABELS,
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
            selector=k8s.LabelSelector(match_labels=_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_LABELS),
                spec=k8s.PodSpec(
                    image_pull_secrets=[k8s.LocalObjectReference(name=SECRET_NAME)],
                    node_selector={"topology.kubernetes.io/region": _REGION},
                    # Stateless: all state is in study-casino-db (CNPG), no local storage. Allow
                    # control-plane nodes as overflow capacity, but prefer workers to keep
                    # ordinary application I/O away from etcd disks.
                    tolerations=[
                        k8s.Toleration(
                            key="node-role.kubernetes.io/control-plane", operator="Exists", effect="NoSchedule"
                        )
                    ],
                    affinity=k8s.Affinity(
                        node_affinity=k8s.NodeAffinity(
                            preferred_during_scheduling_ignored_during_execution=[
                                k8s.PreferredSchedulingTerm(
                                    weight=100,
                                    preference=k8s.NodeSelectorTerm(
                                        match_expressions=[
                                            k8s.NodeSelectorRequirement(
                                                key="node-role.kubernetes.io/control-plane", operator="DoesNotExist"
                                            )
                                        ]
                                    ),
                                )
                            ]
                        )
                    ),
                    containers=[
                        k8s.Container(
                            name="app",
                            image=f"git.allegedly.works/ducktape-ci/study-casino:{_PLACEHOLDER_TAG}",
                            image_pull_policy="Always",
                            ports=[k8s.ContainerPort(name="http", container_port=_PORT, protocol="TCP")],
                            env=[
                                k8s.EnvVar(name="STUDY_CASINO_IMAGE_TAG", value=_PLACEHOLDER_TAG),
                                # Compose the SQLAlchemy URL from the CNPG-generated secret. The
                                # `$(VAR)` syntax in env values is resolved by the kubelet from
                                # earlier-defined env vars in the same container. `+psycopg`
                                # forces SQLAlchemy to pick the psycopg v3 driver.
                                _db_env("PG_USER", "user"),
                                _db_env("PG_PASSWORD", "password"),
                                _db_env("PG_HOST", "host"),
                                _db_env("PG_PORT", "port"),
                                _db_env("PG_DBNAME", "dbname"),
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
                                _oidc_env("STUDY_CASINO_OIDC_CLIENT_SECRET", "oidc_client_secret"),
                                _oidc_env("STUDY_CASINO_SESSION_SECRET", "session_secret"),
                                _oidc_env("STUDY_CASINO_RNG_SECRET", "rng_secret"),
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
        matches=[
            HttpRouteSpecRulesMatches(
                path=HttpRouteSpecRulesMatchesPath(type=HttpRouteSpecRulesMatchesPathType.PATH_PREFIX, value=prefix)
            )
        ],
        filters=[
            HttpRouteSpecRulesFilters(
                type=HttpRouteSpecRulesFiltersType.RESPONSE_HEADER_MODIFIER,
                response_header_modifier=HttpRouteSpecRulesFiltersResponseHeaderModifier(
                    set=[HttpRouteSpecRulesFiltersResponseHeaderModifierSet(name="Cache-Control", value=cache_control)]
                ),
            )
        ],
        backend_refs=[HttpRouteSpecRulesBackendRefs(name=_NAME, port=_PORT)],
    )


def _route(scope: Construct) -> None:
    HttpRoute(
        scope,
        "route",
        metadata=metadata(_NAME, _NAMESPACE),
        spec=HttpRouteSpec(
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
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, _NAME, disable_resource_name_hashes=True)
    _namespace(chart)
    _database(chart)
    _readonly_provisioner(chart)
    forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=_NAMESPACE)
    _deployment(chart)
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=_NAME, namespace=_NAMESPACE),
        spec=k8s.ServiceSpec(
            selector=_LABELS,
            ports=[
                k8s.ServicePort(name="http", port=_PORT, target_port=k8s.IntOrString.from_number(_PORT), protocol="TCP")
            ],
        ),
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


def write_manifests(root: Path) -> None:
    write_charts(root, _OUTPUT_DIR, chart)
