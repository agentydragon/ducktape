"""Exercise the exact dashboard PromQL against clean, failed, missing, and reset probes."""

import json
import subprocess
from pathlib import Path

import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path

_DASHBOARD = "_main/cluster/cdk8s/monitoring/dashboards/home-network-overview.json"


def test_reliability_queries(tmp_path: Path) -> None:
    dashboard = json.loads(get_required_path(_DASHBOARD).read_text())
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
    (tmp_path / "smokeping.yaml").write_text(yaml.safe_dump(spec))
    (tmp_path / "tests.yaml").write_text(
        get_required_path("_main/cluster/cdk8s/monitoring/testdata/smokeping_rules.yaml").read_text()
    )
    subprocess.run(
        [get_required_path("multitool/tools/promtool/promtool"), "test", "rules", "tests.yaml"],
        cwd=tmp_path,
        check=True,
    )


if __name__ == "__main__":
    pytest_bazel.main()
