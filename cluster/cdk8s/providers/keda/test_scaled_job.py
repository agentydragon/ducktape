"""`ScaledJob` renders the required fields under their own names and leaves optional
`ScaledJobSpec` fields unset on `None`, so KEDA's own defaults apply."""

import pytest_bazel
from cdk8s import Testing as Cdk8sTesting
from keda_scaledjob_crds.sh.keda import (
    ScaledJobSpecJobTargetRef,
    ScaledJobSpecJobTargetRefTemplate,
    ScaledJobSpecJobTargetRefTemplateSpec,
    ScaledJobSpecRollout,
    ScaledJobSpecRolloutStrategy,
    ScaledJobSpecTriggers,
)

from cluster.cdk8s.providers.keda.scaled_job import ScaledJob

_JOB_TARGET_REF = ScaledJobSpecJobTargetRef(
    template=ScaledJobSpecJobTargetRefTemplate(spec=ScaledJobSpecJobTargetRefTemplateSpec(containers=[]))
)
_TRIGGERS = [ScaledJobSpecTriggers(type="test-scaler", metadata={"key": "value"})]


def test_required_fields_render_under_their_own_names() -> None:
    chart = Cdk8sTesting.chart()
    ScaledJob(
        chart,
        "scaled-job",
        name="test-scaled-job",
        namespace="test-namespace",
        job_target_ref=_JOB_TARGET_REF,
        triggers=_TRIGGERS,
    )
    (scaled_job,) = Cdk8sTesting.synth(chart)
    assert scaled_job["metadata"]["name"] == "test-scaled-job"
    assert scaled_job["metadata"]["namespace"] == "test-namespace"
    assert scaled_job["spec"]["triggers"] == [{"type": "test-scaler", "metadata": {"key": "value"}}]
    assert "maxReplicaCount" not in scaled_job["spec"]
    assert "rollout" not in scaled_job["spec"]


def test_optional_fields_render_when_given() -> None:
    chart = Cdk8sTesting.chart()
    ScaledJob(
        chart,
        "scaled-job",
        name="test-scaled-job",
        namespace="test-namespace",
        job_target_ref=_JOB_TARGET_REF,
        triggers=_TRIGGERS,
        max_replica_count=4,
        polling_interval=15,
        rollout=ScaledJobSpecRollout(strategy=ScaledJobSpecRolloutStrategy.GRADUAL),
    )
    (scaled_job,) = Cdk8sTesting.synth(chart)
    assert scaled_job["spec"]["maxReplicaCount"] == 4
    assert scaled_job["spec"]["pollingInterval"] == 15
    assert scaled_job["spec"]["rollout"] == {"strategy": "gradual"}


if __name__ == "__main__":
    pytest_bazel.main()
