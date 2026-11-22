"""Exercise the exact dashboard PromQL against clean, failed, missing, and reset probes."""

import json

import pytest_bazel
from python.runfiles import Runfiles

from cluster.cdk8s.testing.promtool import PromtoolRules

_DASHBOARD = "ducktape/cluster/cdk8s/monitoring/dashboards/home-network-overview.json"


def test_reliability_queries(runfiles: Runfiles, promtool: PromtoolRules) -> None:
    runfiles_root = runfiles.root()
    dashboard = json.loads((runfiles_root / _DASHBOARD).read_text())
    expressions = {
        panel["id"]: panel["targets"][0]["expr"] for panel in dashboard["panels"] if panel["id"] in (51, 52, 53)
    }
    affected = expressions[51]
    assert affected.startswith("sum_over_time((")
    assert affected.endswith(")[1h:1m])")
    state = affected[len("sum_over_time((") : -len(")[1h:1m])")]
    spec = {
        "groups": [
            {
                "name": "home-network-query-tests",
                "rules": [
                    {"record": "home_network:icmp_affected:1m", "expr": state},
                    {"record": "home_network:affected_minutes:1h", "expr": expressions[51]},
                    {"record": "home_network:episode_starts:1h", "expr": expressions[52]},
                    {"record": "home_network:observed_minutes:1h", "expr": expressions[53]},
                ],
            }
        ]
    }
    promtool("ducktape/cluster/cdk8s/monitoring/testdata/smokeping_rules.yaml", {"smokeping.yaml": spec})


if __name__ == "__main__":
    pytest_bazel.main()
