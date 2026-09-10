"""Reject overlapping HTTPS termination and TLS passthrough listeners."""

from itertools import combinations

import pytest_bazel
import yaml

from util.bazel.runfiles import get_required_path


def test_cluster_gateway_has_no_cross_protocol_hostname_conflicts() -> None:
    gateway = yaml.safe_load(get_required_path("_main/cluster/k8s/gateway/gateway.yaml").read_text())
    for first, second in combinations(gateway["spec"]["listeners"], 2):
        if first["port"] != second["port"]:
            continue
        if {first["protocol"], second["protocol"]} != {"HTTPS", "TLS"}:
            continue
        tls = first if first["protocol"] == "TLS" else second
        if tls["tls"]["mode"] != "Passthrough":
            continue
        left, right = first.get("hostname", "*"), second.get("hostname", "*")
        overlaps = (
            left == right
            or "*" in (left, right)
            or (left.startswith("*.") and right.endswith(left[1:]))
            or (right.startswith("*.") and left.endswith(right[1:]))
        )
        assert not overlaps, f"Conflicting Gateway listeners: {first['name']} and {second['name']}"


if __name__ == "__main__":
    pytest_bazel.main()
