"""Tests for the default-disable-service-links Kyverno policy."""

from __future__ import annotations

from pathlib import Path

import pytest_bazel
from cdk8s import App

from cluster.cdk8s.kyverno import policies
from cluster.cdk8s.kyverno.apply import apply_policy
from cluster.cdk8s.kyverno.apply_inputs import manifest, policy


def test_pod_gets_service_links_disabled(scratch_app: App, tmp_path: Path) -> None:
    path = policy(policies.default_disable_service_links_chart(scratch_app), tmp_path)
    result = apply_policy(path, manifest("pod_service_links_default.yaml"))
    assert result.ok, result.stdout
    assert result.mutated_resources[0]["spec"]["enableServiceLinks"] is False


if __name__ == "__main__":
    pytest_bazel.main()
