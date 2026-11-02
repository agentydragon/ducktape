"""Probe rolling hosted snapshots through the same lifecycle as the persistent suites."""

import json
import os

from agentplane.system_tests.k3s import stack
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir


def test_k3s_survives_action() -> None:
    binary = get_required_path(os.environ["K3S_BINARY_RLOCATION"])
    fingerprint = stack._baseline_fingerprint(binary, [], [], {}, layers_in_memory=True)
    _k3s, marker, warm = stack._baseline_open(binary, fingerprint, layers_in_memory=True)
    # This bare-cluster probe creates no application resources. The full suite target exercises
    # fixture cleanup before making the same clean-state transition.
    marker["state"] = "clean"
    stack._baseline_write_marker(marker)
    result = {"outcome": "warm" if warm else "cold", "identity": marker["identity"], "fingerprint": fingerprint}
    (undeclared_outputs_dir() / "persistence-probe.json").write_text(json.dumps(result))
    print(json.dumps(result))


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
