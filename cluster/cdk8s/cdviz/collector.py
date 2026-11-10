"""The GitHub event collector and database migrations."""

from cdk8s import ApiObjectMetadata, App, Chart, Size
from cdk8s_plus_34 import Cpu, k8s
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDeletionPolicy
from prometheus_operator_crds.com.coreos.monitoring import ServiceMonitorSpecSelector
from python.runfiles import Runfiles

from cluster.cdk8s import cilium, node_scheduling, pod_policy
from cluster.cdk8s.cdviz import config, timescale
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on_many
from cluster.cdk8s.gateway import https_route
from cluster.cdk8s.providers.cilium.network_policy import EgressRule, IngressRule, NetworkPolicy
from cluster.cdk8s.providers.prometheus_operator.service_monitor import Endpoint, ServiceMonitor

OUTPUT_DIR = f"{config.OUTPUT_DIR}/app"
IMAGE = (
    "ghcr.io/cdviz-dev/cdviz-collector:0.53.1@sha256:7d69848b44d608e56db67b8af6b28885071ae593bcda35bf23cdb220ae45dab8"
)
MIGRATION_IMAGE = (
    "ghcr.io/cdviz-dev/cdviz-db-migration:1.5.0@sha256:6bbc4e2b9e3bfbe283571e80eff2d59ca2e9ecb5d50d51f174535af073b2fb1d"
)
_DATA = "/var/lib/cdviz"
_CONFIG = "/etc/cdviz"
_MIGRATION_RESOURCES = k8s.ResourceRequirements(
    requests={
        "cpu": k8s.Quantity.from_string(Cpu.millis(100).amount),
        "memory": k8s.Quantity.from_string(Size.mebibytes(128).as_string()),
    },
    limits={"memory": k8s.Quantity.from_string(Size.mebibytes(256).as_string())},
)


def _database_env() -> list[k8s.EnvVar]:
    return [
        k8s.EnvVar(name="PGHOST", value=config.DATABASE.rw.host),
        k8s.EnvVar(name="PGPORT", value=str(config.DATABASE.rw.port.number)),
        k8s.EnvVar(name="PGDATABASE", value=config.DATABASE_NAME),
        k8s.EnvVar(name="PGUSER", value=config.DATABASE_NAME),
        config.DATABASE.app_secret.key("password").env_var("PGPASSWORD"),
    ]


def chart(app: App) -> Chart:
    chart = Chart(app, "cdviz-collector", disable_resource_name_hashes=True)
    root = Runfiles.CreateOrRaise().root() / "ducktape/cluster/cdk8s/cdviz"
    files = [
        "collector.toml",
        "github_webhook.vrl",
        "github_workflow_runs.vrl",
        "github_policy.vrl",
        "001_ci_feedback.sql",
        "002_ci_feedback_optimization.sql",
        "grants.sql",
    ]
    k8s.KubeConfigMap(
        chart,
        "config",
        metadata=k8s.ObjectMeta(name="collector-config", namespace=config.NAMESPACE),
        data={name: (root / name).read_text() for name in files},
    )
    k8s.KubePersistentVolumeClaim(
        chart,
        "spool",
        metadata=k8s.ObjectMeta(name="collector-spool", namespace=config.NAMESPACE),
        spec=k8s.PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            storage_class_name="seaweedfs-ovh-ssd",
            resources=k8s.VolumeResourceRequirements(
                requests={"storage": k8s.Quantity.from_string(Size.gibibytes(1).as_string())}
            ),
        ),
    )
    deployment = k8s.KubeDeployment(
        chart,
        "deployment",
        metadata=k8s.ObjectMeta(name=config.COLLECTOR.name, namespace=config.NAMESPACE),
        spec=k8s.DeploymentSpec(
            replicas=1,
            strategy=k8s.DeploymentStrategy(type="Recreate"),
            selector=k8s.LabelSelector(match_labels=config.COLLECTOR.pods.selector),
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels=config.COLLECTOR.pods.selector),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    node_selector=node_scheduling.HIL_OVH.node_selector,
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True, run_as_user=1000, run_as_group=1000, fs_group=1000
                    ),
                    init_containers=[
                        k8s.Container(
                            name="migrate",
                            image=MIGRATION_IMAGE,
                            security_context=k8s.SecurityContext(read_only_root_filesystem=True),
                            resources=_MIGRATION_RESOURCES,
                            args=["-database", "$(DATABASE_URL)", "-path", "/migrations/", "up"],
                            env=[config.DATABASE.app_secret.key("uri").env_var("DATABASE_URL")],
                        ),
                        k8s.Container(
                            name="grants",
                            image=timescale.POSTGRES_IMAGE,
                            security_context=k8s.SecurityContext(read_only_root_filesystem=True),
                            resources=_MIGRATION_RESOURCES,
                            command=["psql"],
                            args=[
                                "--no-psqlrc",
                                "--set=ON_ERROR_STOP=1",
                                "--single-transaction",
                                "--file=/etc/cdviz/001_ci_feedback.sql",
                                "--file=/etc/cdviz/002_ci_feedback_optimization.sql",
                                "--file=/etc/cdviz/grants.sql",
                            ],
                            env=_database_env(),
                            volume_mounts=[k8s.VolumeMount(name="config", mount_path=_CONFIG, read_only=True)],
                        ),
                    ],
                    containers=[
                        k8s.Container(
                            name="collector",
                            image=IMAGE,
                            security_context=k8s.SecurityContext(read_only_root_filesystem=True),
                            args=["connect", "--config", f"{_CONFIG}/collector.toml"],
                            ports=[config.COLLECTOR.port.k8s_container_port()],
                            liveness_probe=k8s.Probe(
                                http_get=k8s.HttpGetAction(
                                    path="/healthz", port=k8s.IntOrString.from_number(config.COLLECTOR.port.number)
                                )
                            ),
                            readiness_probe=k8s.Probe(
                                http_get=k8s.HttpGetAction(
                                    path="/readyz", port=k8s.IntOrString.from_number(config.COLLECTOR.port.number)
                                )
                            ),
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string(Cpu.millis(100).amount),
                                    "memory": k8s.Quantity.from_string(Size.mebibytes(128).as_string()),
                                },
                                limits={"memory": k8s.Quantity.from_string(Size.mebibytes(512).as_string())},
                            ),
                            volume_mounts=[
                                k8s.VolumeMount(name="config", mount_path=_CONFIG, read_only=True),
                                k8s.VolumeMount(name="spool", mount_path=_DATA),
                                k8s.VolumeMount(
                                    name="database", mount_path="/var/run/secrets/database", read_only=True
                                ),
                                k8s.VolumeMount(name="webhook", mount_path="/var/run/secrets/webhook", read_only=True),
                            ],
                        )
                    ],
                    volumes=[
                        k8s.Volume(name="config", config_map=k8s.ConfigMapVolumeSource(name="collector-config")),
                        k8s.Volume(
                            name="spool",
                            persistent_volume_claim=k8s.PersistentVolumeClaimVolumeSource(claim_name="collector-spool"),
                        ),
                        k8s.Volume(
                            name="database", secret=k8s.SecretVolumeSource(secret_name=config.COLLECTOR_PASSWORD.name)
                        ),
                        k8s.Volume(
                            name="webhook", secret=k8s.SecretVolumeSource(secret_name=config.WEBHOOK_SECRET.name)
                        ),
                    ],
                ),
            ),
        ),
    )
    pod_policy.harden(deployment)
    k8s.KubeService(
        chart,
        "service",
        metadata=k8s.ObjectMeta(name=config.COLLECTOR.name, namespace=config.NAMESPACE, labels=config.COLLECTOR.labels),
        spec=k8s.ServiceSpec(selector=config.COLLECTOR.pods.selector, ports=[config.COLLECTOR.port.k8s_service_port()]),
    )
    ServiceMonitor(
        chart,
        "monitor",
        metadata=ApiObjectMetadata(name="collector", namespace=config.NAMESPACE),
        selector=ServiceMonitorSpecSelector(match_labels=config.COLLECTOR.labels),
        endpoints=[Endpoint.plain(port=config.COLLECTOR.port.name)],
    )
    https_route(
        chart,
        "route",
        metadata=ApiObjectMetadata(name="github-webhook", namespace=config.NAMESPACE),
        hostnames=[config.WEBHOOK_HOST],
        backend=config.COLLECTOR,
        paths=[config.WEBHOOK_PATH],
    )
    NetworkPolicy(
        chart,
        "network",
        metadata=ApiObjectMetadata(name="collector", namespace=config.NAMESPACE),
        endpoint_selector=config.COLLECTOR.pods.selector,
        ingress=[
            IngressRule.from_gateway(config.COLLECTOR.port.number),
            cilium.SCRAPERS.admit(config.COLLECTOR.port.number),
        ],
        egress=[
            cilium.dns_egress(resolves=["api.github.com", "**.cluster.local"]),
            config.DATABASE.rw.egress(),
            EgressRule.to_fqdns("api.github.com"),
        ],
    )
    retention = k8s.KubeCronJob(
        chart,
        "retention",
        metadata=k8s.ObjectMeta(name="retention", namespace=config.NAMESPACE),
        spec=k8s.CronJobSpec(
            schedule="30 4 * * *",
            time_zone="Etc/UTC",
            concurrency_policy="Forbid",
            successful_jobs_history_limit=1,
            failed_jobs_history_limit=1,
            job_template=k8s.JobTemplateSpec(
                spec=k8s.JobSpec(
                    backoff_limit=2,
                    active_deadline_seconds=300,
                    template=k8s.PodTemplateSpec(
                        metadata=k8s.ObjectMeta(labels={"app.kubernetes.io/name": "cdviz-retention"}),
                        spec=k8s.PodSpec(
                            restart_policy="Never",
                            automount_service_account_token=False,
                            node_selector=node_scheduling.HIL_OVH.node_selector,
                            security_context=k8s.PodSecurityContext(
                                run_as_non_root=True, run_as_user=1000, run_as_group=1000
                            ),
                            containers=[
                                k8s.Container(
                                    name="retention",
                                    image=timescale.POSTGRES_IMAGE,
                                    command=["psql"],
                                    args=[
                                        "--no-psqlrc",
                                        "--set=ON_ERROR_STOP=1",
                                        "--command=CALL cdviz.apply_retention(INTERVAL '90 days');",
                                    ],
                                    env=_database_env(),
                                    security_context=k8s.SecurityContext(read_only_root_filesystem=True),
                                    resources=k8s.ResourceRequirements(
                                        requests={
                                            "cpu": k8s.Quantity.from_string(Cpu.millis(50).amount),
                                            "memory": k8s.Quantity.from_string(Size.mebibytes(64).as_string()),
                                        },
                                        limits={"memory": k8s.Quantity.from_string(Size.mebibytes(128).as_string())},
                                    ),
                                )
                            ],
                        ),
                    ),
                )
            ),
        ),
    )
    pod_policy.harden(retention)
    NetworkPolicy(
        chart,
        "retention-network",
        metadata=ApiObjectMetadata(name="retention", namespace=config.NAMESPACE),
        endpoint_selector={"app.kubernetes.io/name": "cdviz-retention"},
        egress=[cilium.dns_egress(), config.DATABASE.rw.egress()],
    )
    return chart


def cdviz_app(
    chart: Chart,
    directory: RenderedDirectory,
    database: Kustomization,
    webhook: Kustomization,
    monitoring: Kustomization,
    grafana_operator: Kustomization,
) -> Kustomization:
    return flux_kustomization(
        chart,
        "cdviz",
        directory,
        deletion_policy=KustomizationSpecDeletionPolicy.ORPHAN,
        depends_on=flux_kustomization_depends_on_many(database, webhook, monitoring, grafana_operator),
    )
