"""Tests for the default-disable-service-links Kyverno policy."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import pytest_bazel
from cdk8s import App

from cluster.cdk8s.kyverno.apply import apply_policy
from cluster.cdk8s.kyverno.test_utils import manifest, policy


@pytest.fixture
def service_links_policy(scratch_app: Callable[[], App], tmp_path: Path) -> Path:
    return policy(scratch_app(), "default-disable-service-links", tmp_path)


class TestDefaultDisableServiceLinks:
    def test_pod_gets_service_links_disabled(self, service_links_policy: Path):
        result = apply_policy(service_links_policy, manifest("pod_service_links_default.yaml"))
        assert result.ok, result.stdout
        assert result.passed == 1, result.stdout
        assert result.mutated_resources[0]["spec"]["enableServiceLinks"] is False


if __name__ == "__main__":
    pytest_bazel.main()
