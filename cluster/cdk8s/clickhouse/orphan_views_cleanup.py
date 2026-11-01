"""One-time removal of two orphaned Langfuse views from ClickHouse replicas."""

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ConfigMap, k8s

from cluster.cdk8s import node_scheduling
from cluster.cdk8s.clickhouse import client
from cluster.cdk8s.flux import Kustomization, RenderedDirectory, flux_kustomization, flux_kustomization_depends_on
from cluster.cdk8s.manifest_roots import GENERATED_ROOT
from util.bazel.runfiles import get_required_path, own_repo_rlocation

NAME = "clickhouse-orphan-views-cleanup"
OUTPUT_DIR = f"{GENERATED_ROOT}/{NAME}"
_SCRIPT = get_required_path(own_repo_rlocation("cluster/cdk8s/clickhouse/orphan-views-cleanup.sh")).read_text()


def chart(app: App) -> Chart:
    chart = Chart(app, NAME, disable_resource_name_hashes=True)
    ConfigMap(
        chart, "script", metadata=ApiObjectMetadata(name=NAME, namespace=client.NAMESPACE), data={"cleanup.sh": _SCRIPT}
    )
    k8s.KubeJob(
        chart,
        "job",
        metadata=k8s.ObjectMeta(name=f"{NAME}-v1", namespace=client.NAMESPACE),
        spec=k8s.JobSpec(
            backoff_limit=6,
            template=k8s.PodTemplateSpec(
                metadata=k8s.ObjectMeta(labels={"app.kubernetes.io/name": NAME}),
                spec=k8s.PodSpec(
                    automount_service_account_token=False,
                    enable_service_links=False,
                    restart_policy="OnFailure",
                    node_selector=node_scheduling.HIL_OVH_NODE_SELECTOR,
                    security_context=k8s.PodSecurityContext(
                        run_as_non_root=True,
                        run_as_user=101,
                        run_as_group=101,
                        seccomp_profile=k8s.SeccompProfile(type="RuntimeDefault"),
                    ),
                    containers=[
                        k8s.Container(
                            name="cleanup",
                            image=client.IMAGE,
                            image_pull_policy="IfNotPresent",
                            command=["/bin/sh", "/scripts/cleanup.sh"],
                            env=[
                                client.ADMIN_CREDENTIALS.key("username").env_var("CLICKHOUSE_USER"),
                                client.ADMIN_CREDENTIALS.key(client.PASSWORD_KEY).env_var("CLICKHOUSE_PASSWORD"),
                                k8s.EnvVar(name="HOME", value="/tmp"),
                                k8s.EnvVar(
                                    name="CLICKHOUSE_REPLICA_HOSTS",
                                    value="chi-clickhouse-default-0-0.clickhouse.svc.cluster.local chi-clickhouse-default-0-1.clickhouse.svc.cluster.local",
                                ),
                            ],
                            resources=k8s.ResourceRequirements(
                                requests={
                                    "cpu": k8s.Quantity.from_string("10m"),
                                    "memory": k8s.Quantity.from_string("32Mi"),
                                },
                                limits={
                                    "cpu": k8s.Quantity.from_string("250m"),
                                    "memory": k8s.Quantity.from_string("1Gi"),
                                },
                            ),
                            security_context=k8s.SecurityContext(
                                allow_privilege_escalation=False,
                                read_only_root_filesystem=True,
                                capabilities=k8s.Capabilities(drop=["ALL"]),
                            ),
                            volume_mounts=[
                                k8s.VolumeMount(name="script", mount_path="/scripts", read_only=True),
                                k8s.VolumeMount(name="tmp", mount_path="/tmp"),
                            ],
                        )
                    ],
                    volumes=[
                        k8s.Volume(name="script", config_map=k8s.ConfigMapVolumeSource(name=NAME)),
                        k8s.Volume(name="tmp", empty_dir=k8s.EmptyDirVolumeSource()),
                    ],
                ),
            ),
        ),
    )
    return chart


def cleanup(chart: Chart, directory: RenderedDirectory, operator: Kustomization) -> Kustomization:
    # Do not depend on ClickHouse health: this job unblocks operator schema creation.
    return flux_kustomization(
        chart, NAME, directory, timeout="20m", depends_on=[flux_kustomization_depends_on(operator)]
    )
