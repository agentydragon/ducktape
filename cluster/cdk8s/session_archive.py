"""CNPG projection, retry-safe Restic import jobs, and owner-authenticated archive browser."""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy

from cluster.cdk8s import cilium, cnpg, cnpg_backup, node_scheduling, pod_policy, session_backup
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.forgejo_registry import pull_credentials
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.manifest_roots import GENERATED_ROOT, HAND_WRITTEN_ROOT
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, IngressRule, NetworkPolicy
from cluster.cdk8s.secret_ref import SecretRef
from cluster.cdk8s.service_ref import Pods, Port, ServiceRef
from util.settings_contract import env_name
from x.ai_session_archive.web_settings import WebSettings

NAME = "ai-session-archive"
NAMESPACE = session_backup.NAMESPACE
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"
PINS_DIR = f"{HAND_WRITTEN_ROOT}/ai-session-archive-image-pins"
HOSTNAME = "ai-session-archive.allegedly.works"
DATABASE = cnpg.PostgresRef.generated(name="session-archive-db", namespace=NAMESPACE)
_WEB_IMAGE = "git.allegedly.works/ducktape-ci/ai-session-archive-web:unset"
_IMPORTER_IMAGE = "git.allegedly.works/ducktape-ci/ai-session-archive-importer:unset"
_MIGRATOR_IMAGE = "git.allegedly.works/ducktape-ci/ai-session-archive-migrator:unset"
_OIDC = SecretRef(namespace=NAMESPACE, name="ai-session-archive-oidc")
_RESTIC_CREDENTIALS = SecretRef(namespace=NAMESPACE, name=session_backup.REST_SERVER_AUTH_SECRET)
_RESTIC_PASSWORD_PATH = "/run/restic/password"
_WEB_PORT = 8080
_WEB_LABELS = {"app.kubernetes.io/name": NAME}
_WEB = ServiceRef(name=NAME, port=Port(name="http", number=_WEB_PORT), pods=Pods(NAMESPACE, tuple(_WEB_LABELS.items())))
_IMPORTER_LABELS = session_backup.IMPORTER_LABELS
_IMPORTER_ENDPOINT = cilium.endpoint_labels(NAMESPACE, _IMPORTER_LABELS["app.kubernetes.io/name"])
_CLAUDE_ROOT = "/.claude"
_CODEX_ROOT = "/.codex"
_SOURCE_HOSTS = (
    ("wyrm2", "Wyrm2", "2,17,32,47 * * * *"),
    ("rugged", "Rugged", "7,22,37,52 * * * *"),
    ("iguana", "Iguana", "12,27,42,57 * * * *"),
)
_UID = 1000


def _database(chart: Chart) -> None:
    cnpg.cluster(
        chart,
        "archive-database",
        ref=DATABASE,
        annotations={
            "description": (
                "Imported workstation AI session history, independently protected by CNPG backups and WAL archive."
            )
        },
        profile=cnpg.OVH_PIRAEUS_SSD,
        size="100Gi",
        initdb=cnpg.same_owner_initdb("ai_session_archive"),
        wal_archive=True,
    )
    # Retain weekly base backups and continuous WAL for 30 days; the imported database can outlive
    # source repository retention and must not rely on Restic replay as its only recovery path.
    cnpg_backup.CnpgBackup(
        chart,
        "archive-database-backup",
        ref=DATABASE,
        retention="30d",
        cadence=cnpg_backup.Cadence.WEEKLY,
        adopt_existing=False,
    )


def _migration_container() -> k8s.Container:
    return k8s.Container(
        name="migrate-database",
        image=_MIGRATOR_IMAGE,
        image_pull_policy="IfNotPresent",
        env=[DATABASE.app_secret.key("uri").env_var("DATABASE_URL")],
        resources=k8s.ResourceRequirements(
            requests={"cpu": k8s.Quantity.from_string("50m"), "memory": k8s.Quantity.from_string("128Mi")},
            limits={"memory": k8s.Quantity.from_string("512Mi")},
        ),
    )


def _web_environment() -> list[k8s.EnvVar]:
    return [
        DATABASE.app_secret.key("uri").env_var(env_name(WebSettings, "database_url")),
        k8s.EnvVar(name=env_name(WebSettings, "public_base_url"), value=f"https://{HOSTNAME}"),
        k8s.EnvVar(name=env_name(WebSettings, "port"), value=str(_WEB_PORT)),
        *(
            _OIDC.key(env_name(WebSettings, field)).env_var(env_name(WebSettings, field))
            for field in (
                "oidc_issuer",
                "oidc_client_id",
                "oidc_client_secret",
                "oidc_session_secret",
                "oidc_allowed_subject",
            )
        ),
    ]


def _web(chart: Chart) -> None:
    workload = k8s.KubeDeployment(
        chart,
        "web-deployment",
        metadata=k8s.ObjectMeta(
            name=NAME,
            namespace=NAMESPACE,
            labels=_WEB_LABELS,
            annotations={"description": "Owner-authenticated browser and API for the session archive."},
        ),
        spec=k8s.DeploymentSpec(
            replicas=2,
            selector=k8s.LabelSelector(match_labels=_WEB_LABELS),
            strategy=k8s.DeploymentStrategy(type="RollingUpdate"),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=_WEB_LABELS),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    image_pull_secrets=[k8s.LocalObjectReference(name=pull_credentials.SECRET_NAME)],
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=_UID,
                        run_as_group=_UID,
                        fs_group=_UID,
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    init_containers=[_migration_container()],
                    containers=[
                        k8s.Container(
                            name=NAME,
                            image=_WEB_IMAGE,
                            image_pull_policy="IfNotPresent",
                            env=_web_environment(),
                            ports=[_WEB.port.k8s_container_port()],
                            readiness_probe=k8s.Probe(
                                http_get=k8s.HttpGetAction(
                                    path="/healthz", port=k8s.IntOrString.from_number(_WEB_PORT)
                                ),
                                period_seconds=10,
                            ),
                            liveness_probe=k8s.Probe(
                                http_get=k8s.HttpGetAction(
                                    path="/healthz", port=k8s.IntOrString.from_number(_WEB_PORT)
                                ),
                                initial_delay_seconds=30,
                                period_seconds=20,
                            ),
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("50m"),
                                    "memory": k8s.Quantity.from_string("256Mi"),
                                },
                                limits={"memory": k8s.Quantity.from_string("1Gi")},
                            ),
                            # The Aspect launcher materializes its runtime venv under runfiles at startup.
                            security_context=k8s.SecurityContext(read_only_root_filesystem=False),
                        )
                    ],
                ),
            ),
        ),
    )
    pod_policy.harden(workload)
    pod_policy.place(workload, node_scheduling.HIL_OVH)

    k8s.KubeService(
        chart,
        "web-service",
        metadata=k8s.ObjectMeta(name=_WEB.name, namespace=NAMESPACE, labels=_WEB.labels),
        spec=k8s.ServiceSpec(selector=_WEB.pods.selector, ports=[_WEB.port.k8s_service_port()], type="ClusterIP"),
    )
    https_route(
        chart,
        "web-route",
        metadata=ApiObjectMetadata(name=NAME, namespace=NAMESPACE),
        hostnames=[HOSTNAME],
        backend=_WEB,
        hsts=True,
        listener=None,
    )


def _restic_password_volume(host: str) -> k8s.Volume:
    return k8s.Volume(
        name=f"{host}-restic-password",
        secret=k8s.SecretVolumeSource(
            secret_name=session_backup.REST_SERVER_AUTH_SECRET,
            default_mode=0o440,
            items=[k8s.KeyToPath(key="restic_password", path="password")],
        ),
    )


def _importer_job(chart: Chart, host: str, display_name: str, schedule: str) -> k8s.KubeCronJob:
    labels = {**_IMPORTER_LABELS, "app.kubernetes.io/instance": host}
    password_volume = _restic_password_volume(host)
    job = k8s.KubeCronJob(
        chart,
        f"import-{host}-cronjob",
        metadata=k8s.ObjectMeta(
            name=f"{NAME}-import-{host}",
            namespace=NAMESPACE,
            labels=labels,
            annotations={"description": f"Retry-safe import of every retained {host} Restic snapshot."},
        ),
        spec=k8s.CronJobSpec(
            schedule=schedule,
            concurrency_policy="Forbid",
            successful_jobs_history_limit=1,
            failed_jobs_history_limit=3,
            job_template=k8s.JobTemplateSpec(
                spec=k8s.JobSpec(
                    backoff_limit=2,
                    # Initial replay may span many schedules; a failed job can be retried next cycle.
                    active_deadline_seconds=604800,
                    template=k8s.PodTemplateSpec(
                        metadata=k8s.ObjectMeta(labels=labels),
                        spec=k8s.PodSpec(
                            automount_service_account_token=False,
                            restart_policy="Never",
                            image_pull_secrets=[k8s.LocalObjectReference(name=pull_credentials.SECRET_NAME)],
                            security_context=k8s.PodSecurityContext(
                                run_as_non_root=True,
                                run_as_user=_UID,
                                run_as_group=_UID,
                                fs_group=_UID,
                                seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                            ),
                            init_containers=[_migration_container()],
                            containers=[
                                k8s.Container(
                                    name="import",
                                    image=_IMPORTER_IMAGE,
                                    image_pull_policy="IfNotPresent",
                                    args=[
                                        "--machine-id",
                                        host,
                                        "--display-name",
                                        display_name,
                                        "--claude-root",
                                        _CLAUDE_ROOT,
                                        "--codex-root",
                                        _CODEX_ROOT,
                                    ],
                                    env=[
                                        DATABASE.app_secret.key("uri").env_var("DATABASE_URL"),
                                        k8s.EnvVar(
                                            name="RESTIC_REPOSITORY", value=f"rest:{session_backup.SERVER.url}/{host}"
                                        ),
                                        _RESTIC_CREDENTIALS.key(f"restic_rest_username_{host}").env_var(
                                            "RESTIC_REST_USERNAME"
                                        ),
                                        _RESTIC_CREDENTIALS.key(f"restic_rest_password_{host}").env_var(
                                            "RESTIC_REST_PASSWORD"
                                        ),
                                        k8s.EnvVar(name="RESTIC_PASSWORD_FILE", value=_RESTIC_PASSWORD_PATH),
                                        k8s.EnvVar(name="RESTIC_CACHE_DIR", value="/tmp/restic-cache"),
                                    ],
                                    resources=k8s.ResourceRequirements(
                                        requests={
                                            "cpu": k8s.Quantity.from_string("250m"),
                                            "memory": k8s.Quantity.from_string("512Mi"),
                                        },
                                        limits={
                                            "cpu": k8s.Quantity.from_string("2"),
                                            "memory": k8s.Quantity.from_string("2Gi"),
                                        },
                                    ),
                                    volume_mounts=[
                                        k8s.VolumeMount(
                                            name=password_volume.name, mount_path="/run/restic", read_only=True
                                        )
                                    ],
                                    # The Aspect launcher materializes its runtime venv under runfiles at startup.
                                    security_context=k8s.SecurityContext(read_only_root_filesystem=False),
                                )
                            ],
                            volumes=[password_volume],
                        ),
                    ),
                )
            ),
        ),
    )
    pod_policy.harden(job)
    pod_policy.place(job, node_scheduling.HIL_OVH)
    return job


def _network_policies(chart: Chart) -> None:
    NetworkPolicy(
        chart,
        "web-network-policy",
        metadata=ApiObjectMetadata(name=f"{NAME}-web", namespace=NAMESPACE),
        endpoint_selector=_WEB.pods.cilium,
        ingress=[IngressRule.from_gateway(_WEB.pod_port)],
        egress=[
            cilium.dns_egress(resolves=["*"]),
            cilium.egress_via_gateway("auth.allegedly.works"),
            EgressRule.to_endpoints({"k8s:cnpg.io/cluster": DATABASE.name}, cnpg.PORT.number),
        ],
    )
    NetworkPolicy(
        chart,
        "importer-network-policy",
        metadata=ApiObjectMetadata(name=f"{NAME}-importer", namespace=NAMESPACE),
        endpoint_selector=_IMPORTER_ENDPOINT,
        ingress=[],
        egress=[
            cilium.dns_egress(resolves=["*"]),
            session_backup.SERVER.egress(),
            EgressRule.to_endpoints({"k8s:cnpg.io/cluster": DATABASE.name}, cnpg.PORT.number),
        ],
    )


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    _database(chart)
    pull_credentials.forgejo_images_creds_external_secret(chart, "forgejo-images-creds", namespace=NAMESPACE)
    _web(chart)
    for host, display_name, schedule in _SOURCE_HOSTS:
        _importer_job(chart, host, display_name, schedule)
    _network_policies(chart)
    return chart


def archive(
    chart: Chart,
    directory: RenderedDirectory,
    session_backup_kustomization: Kustomization,
    cnpg_kustomization: Kustomization,
    external_secrets_operator: Kustomization,
    forgejo_images: Kustomization,
    sso_providers: Kustomization,
    seaweedfs_operator: Kustomization,
    forgejo_image_automation: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        NAME,
        directory,
        timeout="30m",
        target_namespace=NAMESPACE,
        depends_on=flux_kustomization_depends_on_many(
            session_backup_kustomization,
            cnpg_kustomization,
            external_secrets_operator,
            forgejo_images,
            sso_providers,
            seaweedfs_operator,
            forgejo_image_automation,
        ),
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        description=(
            "CNPG-backed historical Restic session imports and owner-authenticated browser, with independent 30-day "
            "CNPG backup and WAL retention."
        ),
    )
