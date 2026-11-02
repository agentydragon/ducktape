"""The Attic Nix binary cache (`cache.allegedly.works`): the server and its Postgres,
the previous SeaweedFS identity pending cleanup, the cache bootstrap Job, and hourly JWT rotation.

cdk8s writes the directory Kustomization and the typed rotator roster. The native
`server.toml`, SOPS Secrets, and `image-pins/kustomization.yaml` remain hand-written beside
them; the image-pins Component supplies the rotator image tag (cluster/cdk8s/AGENTS.md §
image-pins).
"""

from __future__ import annotations

from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart, JsonPatch
from cdk8s_plus_34 import ClusterRole, ClusterRolePolicyRule, ServiceAccount, k8s
from cnpg_cluster_crds.io.cnpg.postgresql import ClusterSpecPostgresql
from cnpg_database_crds.io.cnpg.postgresql import DatabaseSpecCluster, DatabaseSpecDatabaseReclaimPolicy
from constructs import Construct
from external_secrets_crds.io.external_secrets import (
    ExternalSecretSpecTargetCreationPolicy,
    ExternalSecretSpecTargetDeletionPolicy,
    ExternalSecretSpecTargetTemplate,
    ExternalSecretSpecTargetTemplateEngineVersion,
)

from cluster.cdk8s import api_resource, cnpg, cnpg_backup, external_creds, namespaces, node_scheduling
from cluster.cdk8s.config_format import yaml_config
from cluster.cdk8s.external_secrets.kubernetes_store import secret_store
from cluster.cdk8s.flux import ConfigMapArgs
from cluster.cdk8s.forgejo_registry import pull_credentials
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.github_secrets_sync import workspace
from cluster.cdk8s.manifest_roots import HAND_WRITTEN_ROOT
from cluster.cdk8s.namespaces import Vpa
from cluster.cdk8s.nix_cache import ci_token
from cluster.cdk8s.providers.cnpg.database import Database
from cluster.cdk8s.providers.external_secrets.external_secret import ExternalSecret, SecretStoreRef, remote_data
from cluster.cdk8s.seaweedfs import s3
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from cluster.rotators.attic_jwt_rotation.config import (
    Config,
    KubernetesDestination,
    KubernetesSecretDestination,
    SopsDestination,
    Token,
    WorkspaceNotification,
)

NAME = "attic"
NAMESPACE = "nix-cache"
HOSTNAME = "cache.allegedly.works"
OUTPUT_DIR = f"{HAND_WRITTEN_ROOT}/nix-cache"
SERVICE = ServiceRef(
    name=NAME,
    port=Port(name="http", number=8080),
    pods=Pods(namespace=NAMESPACE, labels=(("app.kubernetes.io/name", "attic-next"),)),
)
DATABASE = cnpg.PostgresRef.generated(name="attic-db", namespace=NAMESPACE)
_GITHUB_PAT = SecretRef(namespace=NAMESPACE, name="github-secrets-sync-pat")
_ROTATOR = "attic-jwt-rotator"
# Placeholder tag; image-pins/kustomization.yaml sets the real one.
_ROTATOR_IMAGE = "git.allegedly.works/ducktape-ci/attic-jwt-rotation:unset"
_ROTATORS_CONFIG_MAP = "attic-rotators-config"
_NEXT = "attic-next"
_NEXT_CONFIG_MAP = "attic-local-config"
_NEXT_DATABASE_SECRET = "attic-local-database"
_NEXT_STORAGE = "attic-local-storage"
_NEXT_LABELS = {"app.kubernetes.io/name": _NEXT}

ROTATOR_CONFIG = Config(
    attic_deployment="deploy/attic-next",
    tokens=[
        *[
            Token(
                name=f"{sub} attic reader",
                sub=sub,
                validity="1 year",
                pull=["main", "gaffer"],
                destination=SopsDestination(sops_file=Path(secret_file)),
            )
            for sub, secret_file in [
                ("wyrm2", "secrets/hosts/wyrm2-attic.yaml"),
                ("rugged", "secrets/hosts/rugged-attic.yaml"),
                ("iguana", "secrets/hosts/iguana-attic.yaml"),
                ("atlas", "secrets/hosts/atlas-attic.yaml"),
                ("gecko", "secrets/hosts/gecko-attic.yaml"),
                ("agent-box", "secrets/hosts/agent-box-attic.yaml"),
                ("claude-web", "secrets/claude-web-attic.yaml"),
                ("haku", "secrets/haku-attic.yaml"),
            ]
        ],
        # CI reads main and gaffer for Nix substituter access. Public is an
        # anonymous-readable bootstrap cache required before CI credentials exist.
        Token(
            name="ducktape CI attic writer (main)",
            destination=KubernetesDestination(
                secret=KubernetesSecretDestination(
                    namespace=ci_token.WRITER.namespace, name=ci_token.WRITER.name, key=ci_token.WRITER_TOKEN.key
                ),
                notification=WorkspaceNotification(
                    workspace=workspace.NAME, annotation_key="ducktape.allegedly.works/attic-token-version"
                ),
            ),
            sub="ducktape-ci",
            validity="1 year",
            pull=["main", "gaffer"],
            push=["main", "public"],
        ),
        Token(
            name="ducktape CI attic reader",
            destination=KubernetesDestination(
                secret=KubernetesSecretDestination(
                    namespace=ci_token.READER.namespace, name=ci_token.READER.name, key=ci_token.READER_TOKEN.key
                ),
                notification=WorkspaceNotification(
                    workspace=workspace.NAME, annotation_key="ducktape.allegedly.works/attic-reader-token-version"
                ),
            ),
            sub="ducktape-ci-reader",
            validity="1 year",
            pull=["main", "gaffer"],
            push=[],
        ),
        Token(
            name="gaffer CI attic writer",
            sub="gaffer-ci",
            validity="1 year",
            pull=["gaffer"],
            push=["gaffer"],
            destination=KubernetesDestination(
                secret=KubernetesSecretDestination(
                    namespace=ci_token.GAFFER_WRITER.namespace,
                    name=ci_token.GAFFER_WRITER.name,
                    key=ci_token.GAFFER_WRITER_TOKEN.key,
                ),
                notification=WorkspaceNotification(
                    workspace=workspace.NAME, annotation_key="gaffer.allegedly.works/attic-token-version"
                ),
            ),
        ),
    ],
)

NEXT_CONFIG_MAP = ConfigMapArgs(name=_NEXT_CONFIG_MAP, namespace=NAMESPACE, files=["server.toml=server-local.toml"])


ROTATORS_CONFIG_MAP = ConfigMapArgs(
    name=_ROTATORS_CONFIG_MAP,
    namespace=NAMESPACE,
    data={"rotators.yaml": yaml_config(ROTATOR_CONFIG.model_dump(mode="json"))},
)


def _database(scope: Construct) -> None:
    cnpg.cluster(
        scope,
        "db",
        ref=DATABASE,
        image_name=None,
        profile=cnpg.OVH_PIRAEUS_SSD,
        # Leave room for the old and replacement cache databases to coexist during migration.
        size="16Gi",
        resize_in_use_volumes=True,
        instances=3,
        initdb=cnpg.same_owner_initdb("attic"),
        wal_archive=True,
        # Attic autocommits several statements per ~64 KiB chunk. With a commit per fsync on the HDD
        # tier, a large upload saturated the disk until Attic's 10-connection pool timed out (a 500).
        # A crash loses under a second of commits: cache rows a client re-pushes.
        postgresql=ClusterSpecPostgresql(parameters={"synchronous_commit": "off"}),
    )
    cnpg_backup.CnpgBackup(scope, "db-backup", ref=DATABASE, retention="30d")


def _old_cache_identity(scope: Construct) -> None:
    identity = s3.identity(
        scope,
        "old-cache-identity",
        name=NAME,
        namespace=NAMESPACE,
        description="attic's NAR chunks; replicated per volume by the SeaweedFS cluster's defaultReplication.",
    )
    # Remove only after the old bucket and credential have finalized.
    identity.add_json_patch(JsonPatch.replace("/spec/reclaimPolicy", "Delete"))


def _service(scope: Construct) -> None:
    k8s.KubeService(
        scope,
        "service",
        metadata=k8s.ObjectMeta(name=SERVICE.name, namespace=NAMESPACE),
        spec=k8s.ServiceSpec(type="ClusterIP", ports=[SERVICE.port.k8s_service_port()], selector=SERVICE.pods.selector),
    )
    https_route(
        scope,
        "route",
        metadata=ApiObjectMetadata(name=NAMESPACE, namespace=NAMESPACE),
        hostnames=[HOSTNAME],
        backend=SERVICE,
        hsts=False,
        listener=None,
    )


def _local_server(scope: Construct) -> None:
    # CNPG backs up the entire cluster, including this separately owned database.
    Database(
        scope,
        "local-database",
        metadata=ApiObjectMetadata(name=_NEXT_DATABASE_SECRET, namespace=NAMESPACE),
        cluster=DatabaseSpecCluster(name=DATABASE.name),
        name="attic_local",
        owner="attic",
        database_reclaim_policy=DatabaseSpecDatabaseReclaimPolicy.RETAIN,
    )
    reader = k8s.KubeServiceAccount(
        scope, "local-db-reader", metadata=k8s.ObjectMeta(name="attic-local-db-reader", namespace=NAMESPACE)
    )
    k8s.KubeRole(
        scope,
        "local-db-reader-role",
        metadata=k8s.ObjectMeta(name="attic-local-db-reader", namespace=NAMESPACE),
        rules=[
            k8s.PolicyRule(
                api_groups=[""], resources=["secrets"], resource_names=[DATABASE.app_secret.name], verbs=["get"]
            )
        ],
    )
    k8s.KubeRoleBinding(
        scope,
        "local-db-reader-binding",
        metadata=k8s.ObjectMeta(name="attic-local-db-reader", namespace=NAMESPACE),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name="attic-local-db-reader"),
        subjects=[k8s.Subject(kind="ServiceAccount", name="attic-local-db-reader", namespace=NAMESPACE)],
    )
    secret_store(
        scope,
        "local-db-store",
        metadata=ApiObjectMetadata(name=_NEXT_DATABASE_SECRET, namespace=NAMESPACE),
        reader=reader,
    )
    ExternalSecret(
        scope,
        "local-db-secret",
        metadata=ApiObjectMetadata(name=_NEXT_DATABASE_SECRET, namespace=NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=SecretStoreRef.namespaced(_NEXT_DATABASE_SECRET),
        data=[remote_data(DATABASE.app_secret.name, "uri")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        template=ExternalSecretSpecTargetTemplate(
            engine_version=ExternalSecretSpecTargetTemplateEngineVersion.V2,
            data={
                "uri": '{{ if hasSuffix "/attic" .uri }}{{ trimSuffix "/attic" .uri }}/attic_local{{ else }}{{ fail "unexpected Attic database URI" }}{{ end }}'
            },
        ),
    )
    k8s.KubePersistentVolumeClaim(
        scope,
        "local-storage",
        metadata=k8s.ObjectMeta(name=_NEXT_STORAGE, namespace=NAMESPACE),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="piraeus-hdd-2",
            resources=k8s.VolumeResourceRequirements(requests={"storage": k8s.Quantity.from_string("100Gi")}),
        ),
    )
    k8s.KubeDeployment(
        scope,
        "candidate-deployment",
        metadata=k8s.ObjectMeta(name=_NEXT, namespace=NAMESPACE),
        spec=k8s.DeploymentSpec(
            replicas=1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=_NEXT_LABELS),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_NEXT_LABELS),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    # Auto-diskful needs the consumer to have the HDD pool locally.
                    node_selector={**node_scheduling.HIL_OVH_NODE_SELECTOR, "storage.allegedly.works/tier": "hdd"},
                    tolerations=[node_scheduling.CONTROL_PLANE_TOLERATION],
                    security_context=k8s.PodSecurityContext(fs_group=1000),
                    containers=[
                        k8s.Container(
                            name=NAME,
                            # Match the running server, so testing the new backend does not upgrade Attic.
                            image="ghcr.io/zhaofengli/attic@sha256:18574aba70fc89d2b695273fbe2e7b2f8ad7e8e786b4cc535124fbe14bada1d0",
                            security_context=k8s.SecurityContext(
                                run_as_user=1000,
                                run_as_group=1000,
                                run_as_non_root=True,
                                allow_privilege_escalation=False,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                                seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                            ),
                            env=[
                                SecretRef(namespace=NAMESPACE, name=_NEXT_DATABASE_SECRET)
                                .key("uri")
                                .env_var("ATTIC_SERVER_DATABASE_URL"),
                                SecretRef(namespace=NAMESPACE, name="attic-jwt-token")
                                .key("jwt-token")
                                .env_var("ATTIC_SERVER_TOKEN_HS256_SECRET_BASE64"),
                            ],
                            args=["-f", "/config/server.toml", "--mode", "monolithic"],
                            ports=[SERVICE.port.k8s_container_port()],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("50m"),
                                    "memory": k8s.Quantity.from_string("1280Mi"),
                                },
                                limits={"memory": k8s.Quantity.from_string("4Gi")},
                            ),
                            volume_mounts=[
                                k8s.VolumeMount(name="config", mount_path="/config", read_only=True),
                                k8s.VolumeMount(name="tmp", mount_path="/tmp"),
                                k8s.VolumeMount(name="storage", mount_path="/data"),
                            ],
                            liveness_probe=k8s.Probe(
                                http_get=k8s.HttpGetAction(path="/", port=k8s.IntOrString.from_string("http")),
                                initial_delay_seconds=10,
                                period_seconds=30,
                                timeout_seconds=5,
                            ),
                            readiness_probe=k8s.Probe(
                                http_get=k8s.HttpGetAction(path="/", port=k8s.IntOrString.from_string("http")),
                                initial_delay_seconds=5,
                                period_seconds=10,
                                timeout_seconds=3,
                            ),
                        )
                    ],
                    volumes=[
                        k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name=_NEXT_CONFIG_MAP)),
                        k8s.Volume(name="tmp", empty_dir=k8s.EmptyDirVolumeSource()),
                        k8s.Volume(
                            name="storage",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name=_NEXT_STORAGE),
                        ),
                    ],
                ),
            ),
        ),
    )
    k8s.KubeService(
        scope,
        "candidate-service",
        metadata=k8s.ObjectMeta(name=_NEXT, namespace=NAMESPACE),
        spec=k8s.ServiceSpec(type="ClusterIP", ports=[SERVICE.port.k8s_service_port()], selector=_NEXT_LABELS),
    )
    k8s.KubeJob(
        scope,
        "candidate-bootstrap",
        metadata=k8s.ObjectMeta(
            name="attic-next-bootstrap",
            namespace=NAMESPACE,
            annotations={"kustomize.toolkit.fluxcd.io/force": "enabled"},
        ),
        spec=k8s.JobSpec(
            backoff_limit=20,
            template=k8s.PodTemplateSpec(
                spec=k8s.PodSpec(
                    service_account_name=_ROTATOR,
                    automount_service_account_token=True,
                    image_pull_secrets=[k8s.LocalObjectReference(name=pull_credentials.SECRET_NAME)],
                    restart_policy="OnFailure",
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=65534,
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[
                        k8s.Container(
                            name="bootstrap",
                            image=_ROTATOR_IMAGE,
                            args=[
                                "bootstrap-caches",
                                "--server=http://attic-next.nix-cache.svc.cluster.local:8080",
                                "--attic-deployment=deploy/attic-next",
                                "--cache=main",
                                "--cache=gaffer",
                                "--public-cache=public",
                                "--keypair-dir=/secrets/cache-keys",
                            ],
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
                            ),
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("100m"),
                                    "memory": k8s.Quantity.from_string("128Mi"),
                                },
                                limits={"memory": k8s.Quantity.from_string("256Mi")},
                            ),
                            volume_mounts=[
                                k8s.VolumeMount(name="cache-keys", mount_path="/secrets/cache-keys", read_only=True)
                            ],
                        )
                    ],
                    volumes=[
                        k8s.Volume(
                            name="cache-keys",
                            secret=k8s.SecretVolumeSource(
                                secret_name="attic-cache-keys",
                                items=[k8s.KeyToPath(key=name, path=name) for name in ("main", "gaffer", "public")],
                            ),
                        )
                    ],
                )
            ),
        ),
    )


def _rotation(scope: Construct) -> None:
    # Flux owns the Secret's identity; only the rotator owns its data. Omitting
    # data and using Merge preserve rotator-owned fields on Flux reconciles.
    k8s.KubeSecret(
        scope,
        "ci-writer-token",
        metadata=k8s.ObjectMeta(
            name=ci_token.WRITER.name,
            namespace=ci_token.WRITER.namespace,
            annotations={"kustomize.toolkit.fluxcd.io/ssa": "Merge"},
        ),
        type="Opaque",
    )
    k8s.KubeSecret(
        scope,
        "ci-reader-token",
        metadata=k8s.ObjectMeta(
            name=ci_token.READER.name,
            namespace=ci_token.READER.namespace,
            annotations={"kustomize.toolkit.fluxcd.io/ssa": "Merge"},
        ),
        type="Opaque",
    )
    k8s.KubeSecret(
        scope,
        "gaffer-ci-writer-token",
        metadata=k8s.ObjectMeta(
            name=ci_token.GAFFER_WRITER.name,
            namespace=ci_token.GAFFER_WRITER.namespace,
            annotations={"kustomize.toolkit.fluxcd.io/ssa": "Merge"},
        ),
        type="Opaque",
    )
    notification_role = ClusterRole(
        scope,
        "ci-token-notification",
        metadata=ApiObjectMetadata(name="attic-github-secrets-notification"),
        rules=[
            ClusterRolePolicyRule(
                endpoints=[api_resource.named_resource("opentofu.upbound.io", "workspaces", workspace.NAME)],
                verbs=["get", "patch"],
            )
        ],
    )
    notification_role.bind(
        ServiceAccount.from_service_account_name(scope, "notification-identity", _ROTATOR, namespace_name=NAMESPACE)
    )
    k8s.KubeServiceAccount(
        scope, "external-creds-reader", metadata=k8s.ObjectMeta(name="external-creds-reader", namespace=NAMESPACE)
    )
    # The rotator's GitHub PAT, copied from the canonical external-creds source
    # (external_creds.py approves nix-cache as a consumer).
    ExternalSecret(
        scope,
        "github-pat",
        metadata=ApiObjectMetadata(name=_GITHUB_PAT.name, namespace=NAMESPACE),
        refresh_interval="1h",
        secret_store_ref=external_creds.STORE,
        data=[remote_data("github-agentydragon-2", "token")],
        creation_policy=ExternalSecretSpecTargetCreationPolicy.OWNER,
        deletion_policy=ExternalSecretSpecTargetDeletionPolicy.RETAIN,
        template=ExternalSecretSpecTargetTemplate(type="Opaque"),
    )
    k8s.KubeServiceAccount(
        scope,
        "rotator",
        metadata=k8s.ObjectMeta(
            name=_ROTATOR,
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Runs the Attic JWT rotation CronJobs. RBAC is `pods/exec` scoped to the nix-cache namespace so"
                    " the rotator can `kubectl exec deploy/attic-next -- atticadm make-token …` without ever holding the"
                    " HS256 signing secret."
                )
            },
        ),
        image_pull_secrets=[k8s.LocalObjectReference(name=pull_credentials.SECRET_NAME)],
    )
    k8s.KubeRole(
        scope,
        "rotator-role",
        metadata=k8s.ObjectMeta(name=_ROTATOR, namespace=NAMESPACE),
        rules=[
            # `kubectl exec` requires get on pods (to resolve the deployment to a Pod name) plus
            # create on pods/exec (to open the exec stream).
            k8s.PolicyRule(api_groups=[""], resources=["pods"], verbs=["get", "list"]),
            k8s.PolicyRule(api_groups=[""], resources=["pods/exec"], verbs=["create"]),
            k8s.PolicyRule(
                api_groups=[""],
                resources=["secrets"],
                resource_names=[ci_token.GAFFER_WRITER.name, ci_token.READER.name, ci_token.WRITER.name],
                verbs=["get", "patch"],
            ),
            # Resolving the deployment to a pod first reads the Deployment.
            k8s.PolicyRule(api_groups=["apps"], resources=["deployments"], verbs=["get"]),
        ],
    )
    k8s.KubeRoleBinding(
        scope,
        "rotator-binding",
        metadata=k8s.ObjectMeta(name=_ROTATOR, namespace=NAMESPACE),
        role_ref=k8s.RoleRef(api_group="rbac.authorization.k8s.io", kind="Role", name=_ROTATOR),
        subjects=[k8s.Subject(kind="ServiceAccount", name=_ROTATOR, namespace=NAMESPACE)],
    )
    k8s.KubeCronJob(
        scope,
        "rotation",
        metadata=k8s.ObjectMeta(
            name="attic-jwt-rotation",
            namespace=NAMESPACE,
            annotations={
                "description": (
                    "Rotates all Attic JWTs (per-host readers, claude-web reader, ducktape CI reader and writer, and"
                    " gaffer CI writer"
                    " tokens) defined in rotators.yaml. Hourly cron; per-token freshness check skips entries with"
                    " >24h remaining, so a real mint runs ~once per token per year. See rotate.py."
                )
            },
        ),
        spec=k8s.CronJobSpec(
            schedule="30 * * * *",
            successful_jobs_history_limit=3,
            failed_jobs_history_limit=3,
            job_template=k8s.JobTemplateSpec(
                spec=k8s.JobSpec(
                    # A Job whose pod can never start (e.g. it pins a kustomize-hashed ConfigMap
                    # that a newer generation pruned) stays active forever -- history limits and
                    # TTL apply only to finished Jobs. The deadline fails it; the TTL then
                    # garbage-collects it.
                    active_deadline_seconds=1800,
                    ttl_seconds_after_finished=86400,
                    backoff_limit=2,
                    template=k8s.PodTemplateSpec(
                        spec=k8s.PodSpec(
                            service_account_name=_ROTATOR,
                            # The rotator runs `kubectl exec` against the active server with this token.
                            automount_service_account_token=True,
                            restart_policy="OnFailure",
                            volumes=[
                                k8s.Volume(
                                    name="rotators-config",
                                    config_map=k8s.ConfigMapVolumeSource(name=_ROTATORS_CONFIG_MAP),
                                )
                            ],
                            security_context=k8s.PodSecurityContext(
                                run_as_non_root=True,
                                run_as_user=65534,
                                seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                            ),
                            containers=[
                                k8s.Container(
                                    name="rotate",
                                    image=_ROTATOR_IMAGE,
                                    args=["rotate", "--config", "/config/rotators.yaml"],
                                    env=[_GITHUB_PAT.key("token").env_var("GIT_TOKEN")],
                                    security_context=k8s.SecurityContext(
                                        allow_privilege_escalation=False, capabilities=k8s.Capabilities(drop=["ALL"])
                                    ),
                                    volume_mounts=[
                                        k8s.VolumeMount(name="rotators-config", mount_path="/config", read_only=True)
                                    ],
                                    resources=k8s.ResourceRequirements(
                                        requests={
                                            "cpu": k8s.Quantity.from_string("100m"),
                                            "memory": k8s.Quantity.from_string("256Mi"),
                                        },
                                        limits={"memory": k8s.Quantity.from_string("512Mi")},
                                    ),
                                )
                            ],
                        )
                    ),
                )
            ),
        ),
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    namespaces.namespace(chart, "namespace", name=NAMESPACE, vpa=Vpa.AUTO)
    pull_credentials.forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAMESPACE)
    _database(chart)
    _old_cache_identity(chart)
    _service(chart)
    _rotation(chart)
    _local_server(chart)
    return chart
