"""Exercise the central proxy's alert expressions with upstream promtool."""

import pytest_bazel
from cdk8s import (
    App,
    Testing as Cdk8sTesting,  # pytest auto-collects classes named Test*
)
from more_itertools import one

from cluster.cdk8s.testing.promtool import PromtoolRules
from cluster.cdk8s.x.github_api_proxy import proxy


def test_github_proxy_rules(promtool: PromtoolRules, scratch_app: App) -> None:
    manifest = one(
        manifest
        for manifest in Cdk8sTesting.synth(proxy.app_chart(scratch_app))
        if manifest["kind"] == "PrometheusRule"
    )
    promtool(
        "ducktape/cluster/cdk8s/x/github_api_proxy/testdata/github_proxy_rules.yaml",
        {"github-proxy.yaml": manifest["spec"]},
    )


if __name__ == "__main__":
    pytest_bazel.main()
