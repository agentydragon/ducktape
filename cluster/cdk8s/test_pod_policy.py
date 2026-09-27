from typing import Any

import pytest
import pytest_bazel
from cdk8s import (  # pytest auto-collects classes named Test*
    ApiObject,
    ApiObjectMetadata,
    Chart,
    Testing as Cdk8sTesting,
)
from cdk8s_plus_34 import ContainerSecurityContextProps, Deployment, k8s
from keda_scaledjob_crds.sh import keda

from cluster.cdk8s import pod_policy
from cluster.cdk8s.node_scheduling import Placement

_PLACEMENT = Placement(node_selector={"example.test/zone": "zone-a"})


@pytest.fixture
def chart() -> Chart:
    return Chart(Cdk8sTesting.app(), "test")


def _deployment(chart: Chart) -> Deployment:
    deployment = Deployment(chart, "deployment", metadata=ApiObjectMetadata(name="app"))
    deployment.add_container(
        name="app", image="app.example.test/app", security_context=ContainerSecurityContextProps(user=1000)
    )
    return deployment


def _cron_job(chart: Chart, *, tolerations: list[k8s.Toleration] | None = None) -> k8s.KubeCronJob:
    pod = k8s.PodSpec(
        init_containers=[k8s.Container(name="init", image="init.example.test/init")],
        containers=[k8s.Container(name="main", image="main.example.test/main")],
        tolerations=tolerations,
    )
    return k8s.KubeCronJob(
        chart,
        "cron-job",
        metadata=k8s.ObjectMeta(name="cron"),
        spec=k8s.CronJobSpec(
            schedule="@daily",
            job_template=k8s.JobTemplateSpec(spec=k8s.JobSpec(template=k8s.PodTemplateSpec(spec=pod))),
        ),
    )


def _scaled_job(chart: Chart) -> keda.ScaledJob:
    template = keda.ScaledJobSpecJobTargetRefTemplateSpec(
        security_context=keda.ScaledJobSpecJobTargetRefTemplateSpecSecurityContext(run_as_user=1000),
        containers=[
            keda.ScaledJobSpecJobTargetRefTemplateSpecContainers(
                name="runner",
                security_context=keda.ScaledJobSpecJobTargetRefTemplateSpecContainersSecurityContext(
                    allow_privilege_escalation=True,
                    capabilities=keda.ScaledJobSpecJobTargetRefTemplateSpecContainersSecurityContextCapabilities(
                        add=["NET_BIND_SERVICE"]
                    ),
                ),
            )
        ],
    )
    return keda.ScaledJob(
        chart,
        "scaled-job",
        metadata=ApiObjectMetadata(name="runner"),
        spec=keda.ScaledJobSpec(
            job_target_ref=keda.ScaledJobSpecJobTargetRef(
                template=keda.ScaledJobSpecJobTargetRefTemplate(spec=template)
            ),
            triggers=[],
        ),
    )


def _deployment_pod(deployment: Deployment) -> dict[str, Any]:
    pod: dict[str, Any] = ApiObject.of(deployment).to_json()["spec"]["template"]["spec"]
    return pod


def test_harden_fills_only_what_each_dialect_leaves_unset(chart: Chart) -> None:
    deployment, cron_job, scaled_job = _deployment(chart), _cron_job(chart), _scaled_job(chart)
    for workload in (deployment, cron_job, scaled_job):
        pod_policy.harden(workload)

    runtime_default, drop_all = {"type": "RuntimeDefault"}, {"drop": ["ALL"]}
    deployment_pod = _deployment_pod(deployment)
    assert deployment_pod["securityContext"]["seccompProfile"] == runtime_default
    # A patch into a cdk8s-plus container's securityContext survives rendering.
    assert deployment_pod["containers"][0]["securityContext"]["capabilities"] == drop_all
    cron_pod = cron_job.to_json()["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert cron_pod["securityContext"] == {"seccompProfile": runtime_default}
    assert [c["securityContext"] for c in [*cron_pod["initContainers"], *cron_pod["containers"]]] == [
        {"allowPrivilegeEscalation": False, "capabilities": drop_all}
    ] * 2
    # What the ScaledJob states at construction survives.
    scaled_pod = scaled_job.to_json()["spec"]["jobTargetRef"]["template"]["spec"]
    assert scaled_pod["securityContext"] == {"runAsUser": 1000, "seccompProfile": runtime_default}
    assert scaled_pod["containers"][0]["securityContext"] == {
        "allowPrivilegeEscalation": True,
        "capabilities": {"add": ["NET_BIND_SERVICE"]},
    }


def test_place_requires_the_placement_and_appends_the_control_plane_toleration(chart: Chart) -> None:
    deployment = _deployment(chart)
    existing = k8s.Toleration(key="example.test/roaming", operator="Exists")
    cron_job = _cron_job(chart, tolerations=[existing])
    pod_policy.place(deployment, _PLACEMENT)
    pod_policy.place(cron_job, _PLACEMENT, tolerate_control_plane=True)

    required = {"key": "example.test/zone", "operator": "In", "values": ["zone-a"]}
    cron_pod = cron_job.to_json()["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    for pod in (_deployment_pod(deployment), cron_pod):
        assert pod["affinity"] == {
            "nodeAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": {
                    "nodeSelectorTerms": [{"matchExpressions": [required]}]
                }
            }
        }
    assert "tolerations" not in _deployment_pod(deployment)
    assert cron_pod["tolerations"] == [
        {"key": "example.test/roaming", "operator": "Exists"},
        {"key": "node-role.kubernetes.io/control-plane", "operator": "Exists", "effect": "NoSchedule"},
    ]


def test_place_refuses_a_placed_pod_and_both_refuse_an_unknown_kind(chart: Chart) -> None:
    deployment = _deployment(chart)
    pod_policy.place(deployment, _PLACEMENT)
    with pytest.raises(ValueError, match="Deployment/app: already placed"):
        pod_policy.place(deployment, _PLACEMENT)
    service = k8s.KubeService(chart, "service", metadata=k8s.ObjectMeta(name="app"))
    with pytest.raises(KeyError):
        pod_policy.harden(service)


if __name__ == "__main__":
    pytest_bazel.main()
