"""Tests for the default-disable-service-links Kyverno policy."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.kyverno.apply import apply_policy
from cluster.validation.kyverno.paths import manifest, policy


@pytest.fixture
def service_links_policy() -> Path:
    return policy("default-disable-service-links.yaml")


class TestDefaultDisableServiceLinks:
    def test_pod_gets_service_links_disabled(self, service_links_policy: Path):
        result = apply_policy(service_links_policy, manifest("pod_service_links_default.yaml"))
        assert result.ok, result.stdout
        assert result.passed == 1, result.stdout
        assert result.mutated_resources[0]["spec"]["enableServiceLinks"] is False

    def test_token_provisioner_is_explicit_exception(self, service_links_policy: Path):
        result = apply_policy(service_links_policy, manifest("pod_service_links_exception.yaml"))
        assert result.ok, result.stdout
        assert result.passed == 0, result.stdout
        assert result.failed == 0, result.stdout


if __name__ == "__main__":
    pytest_bazel.main()
