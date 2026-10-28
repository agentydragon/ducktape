"""Manual, same-client API and Git benchmarks for GitLab and the current Forgejo.

The CronJobs stay suspended. An operator creates one Job from each CronJob for a
comparison, then collects the CSV emitted in each Job's log.
"""

from __future__ import annotations

from cdk8s import ApiObjectMetadata, App, Chart
from cdk8s_plus_34 import ConfigMap, k8s
from cilium_crds.io.cilium import (
    CiliumNetworkPolicySpecEgressDeny,
    CiliumNetworkPolicySpecEgressDenyToEntities,
    CiliumNetworkPolicySpecIngressDeny,
    CiliumNetworkPolicySpecIngressDenyFromEntities,
)

from cluster.cdk8s import pod_policy
from cluster.cdk8s.gitlab_spike import app
from cluster.cdk8s.providers.cilium.network_policy import NetworkPolicy
from util.bazel.runfiles import get_required_path, own_repo_rlocation

NAME = "gitlab-spike-benchmark"
_FORGEJO_NAME = "forgejo-spike-benchmark"
_SCRIPTS = "gitlab-spike-benchmark-scripts"
# The v0.13 Dockerfile installs git, curl and jq; both scripts use this exact client image.
# https://github.com/nicolaka/netshoot/blob/v0.13/Dockerfile
_IMAGE = "nicolaka/netshoot:v0.13@sha256:a20c2531bf35436ed3766cd6cfe89d352b050ccc4d7005ce6400adf97503da1b"
_WORKER = "ovh-ns102453"


def _scripts(chart: Chart) -> None:
    ConfigMap(
        chart,
        "scripts",
        metadata=ApiObjectMetadata(name=_SCRIPTS, namespace=app.NAMESPACE),
        data={
            "gitlab-bench.sh": get_required_path(
                own_repo_rlocation("cluster/debug/gitlab_gitaly_storage_bench/gitlab-bench.sh")
            ).read_text(),
            "forgejo-bench.sh": get_required_path(
                own_repo_rlocation("cluster/debug/piraeus_migration/benchmarks/forgejo-bench.sh")
            ).read_text(),
        },
    )


def _cronjob(
    chart: Chart, *, name: str, script: str, target: str, endpoint: str, credentials: list[k8s.EnvVar]
) -> None:
    cronjob = k8s.KubeCronJob(
        chart,
        name,
        metadata=k8s.ObjectMeta(name=name, namespace=app.NAMESPACE),
        spec=k8s.CronJobSpec(
            # Kubernetes requires a schedule even for a permanently manual CronJob.
            schedule="0 0 1 1 *",
            suspend=True,
            concurrency_policy="Forbid",
            successful_jobs_history_limit=1,
            failed_jobs_history_limit=1,
            job_template=k8s.JobTemplateSpec(
                spec=k8s.JobSpec(
                    active_deadline_seconds=1800,
                    backoff_limit=0,
                    template=k8s.PodTemplateSpec(
                        metadata=k8s.ObjectMeta(labels={"app.kubernetes.io/name": name}),
                        spec=k8s.PodSpec(
                            automount_service_account_token=False,
                            restart_policy="Never",
                            node_selector={"kubernetes.io/hostname": _WORKER},
                            security_context=k8s.PodSecurityContext(
                                run_as_non_root=True, run_as_user=1000, run_as_group=1000, fs_group=1000
                            ),
                            volumes=[
                                k8s.Volume(name="scripts", config_map=k8s.ConfigMapVolumeSource(name=_SCRIPTS)),
                                k8s.Volume(name="tmp", empty_dir=k8s.EmptyDirVolumeSource()),
                            ],
                            containers=[
                                k8s.Container(
                                    name="benchmark",
                                    image=_IMAGE,
                                    image_pull_policy="IfNotPresent",
                                    command=["/bin/sh", f"/scripts/{script}"],
                                    working_dir="/tmp",
                                    env=[
                                        k8s.EnvVar(name="TARGET_NAME", value=target),
                                        k8s.EnvVar(
                                            name=f"{endpoint}_URL",
                                            value=app.WEB.url
                                            if endpoint == "GITLAB"
                                            else "http://forgejo-http.forgejo:3000",
                                        ),
                                        k8s.EnvVar(name="HOME", value="/tmp"),
                                        *credentials,
                                    ],
                                    volume_mounts=[
                                        k8s.VolumeMount(name="scripts", mount_path="/scripts", read_only=True),
                                        k8s.VolumeMount(name="tmp", mount_path="/tmp"),
                                    ],
                                    resources=k8s.ResourceRequirements(
                                        requests={
                                            "cpu": k8s.Quantity.from_string("100m"),
                                            "memory": k8s.Quantity.from_string("128Mi"),
                                        },
                                        limits={"memory": k8s.Quantity.from_string("1Gi")},
                                    ),
                                    security_context=k8s.SecurityContext(read_only_root_filesystem=True),
                                )
                            ],
                        ),
                    ),
                )
            ),
        ),
    )
    pod_policy.harden(cronjob)


def chart(app_instance: App) -> Chart:
    chart = Chart(app_instance, NAME, disable_resource_name_hashes=True)
    NetworkPolicy(
        chart,
        "fault-isolation",
        metadata=ApiObjectMetadata(name="gitlab-spike-fault-isolation", namespace=app.NAMESPACE),
        endpoint_selector={"app": "gitaly", "release": app.NAME, "gitlab-spike.ducktape.io/fault": "isolated"},
        ingress_deny=[
            CiliumNetworkPolicySpecIngressDeny(from_entities=[CiliumNetworkPolicySpecIngressDenyFromEntities.ALL])
        ],
        egress_deny=[CiliumNetworkPolicySpecEgressDeny(to_entities=[CiliumNetworkPolicySpecEgressDenyToEntities.ALL])],
    )
    _scripts(chart)
    _cronjob(
        chart,
        name=NAME,
        script="gitlab-bench.sh",
        target="gitlab-spike",
        endpoint="GITLAB",
        credentials=[
            k8s.EnvVar(
                name="GITLAB_TOKEN",
                value_from=k8s.EnvVarSource(
                    secret_key_ref=k8s.SecretKeySelector(name="gitlab-spike-benchmark-token", key="token")
                ),
            )
        ],
    )
    _cronjob(
        chart,
        name=_FORGEJO_NAME,
        script="forgejo-bench.sh",
        target="forgejo-current",
        endpoint="FORGEJO",
        credentials=[
            k8s.EnvVar(
                name=f"FORGEJO_{key.upper()}",
                value_from=k8s.EnvVarSource(
                    secret_key_ref=k8s.SecretKeySelector(name="forgejo-spike-benchmark", key=key)
                ),
            )
            for key in ("username", "password")
        ],
    )
    return chart
