"""haku-ci's cdk8s model wires the Forgejo token Secret into KEDA's runner scaler."""

from __future__ import annotations

import pytest_bazel
from cdk8s import App, Testing as Cdk8sTesting

from cluster.cdk8s.haku_ci import runner


def test_keda_authentication_and_scaled_job_are_wired(scratch_app: App) -> None:
    resources = Cdk8sTesting.synth(runner.chart(scratch_app))

    authentication = next(resource for resource in resources if resource["kind"] == "TriggerAuthentication")
    assert authentication["metadata"]["name"] == "forgejo"
    assert authentication["metadata"]["namespace"] == runner.NAMESPACE
    assert authentication["spec"]["secretTargetRef"] == [
        {"parameter": "token", "name": runner.FORGEJO_TOKEN_SECRET, "key": runner.FORGEJO_TOKEN_KEY}
    ]

    scaled_job = next(resource for resource in resources if resource["kind"] == "ScaledJob")
    assert scaled_job["metadata"]["namespace"] == runner.NAMESPACE
    (trigger,) = scaled_job["spec"]["triggers"]
    assert trigger["type"] == "forgejo-runner"
    assert trigger["metadata"] == {
        "address": "http://forgejo-http.forgejo:3000",
        "owner": "haku",
        "repo": "haku-state",
        "labels": "haku-ci",
    }
    assert trigger["authenticationRef"] == {"name": authentication["metadata"]["name"]}


if __name__ == "__main__":
    pytest_bazel.main()
