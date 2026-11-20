"""Tests for the require-gitops Kyverno policy."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel
from cdk8s import App

from cluster.cdk8s.kyverno.apply import apply_policy
from cluster.cdk8s.kyverno.test_utils import manifest, policy


@pytest.fixture
def require_gitops_policy(scratch_app: App, tmp_path: Path) -> Path:
    return policy(scratch_app, "require-gitops", tmp_path)


class TestRequireGitops:
    """Matches on `request.userInfo`, which `kyverno apply` takes from `--userinfo`."""

    def test_human_is_reported(self, require_gitops_policy: Path):
        result = apply_policy(
            require_gitops_policy,
            manifest("deployment_require_gitops.yaml"),
            userinfo_path=manifest("userinfo_human.yaml"),
        )
        assert result.failed == 1, result.stdout

    def test_flux_controller_is_exempt(self, require_gitops_policy: Path):
        result = apply_policy(
            require_gitops_policy,
            manifest("deployment_require_gitops.yaml"),
            userinfo_path=manifest("userinfo_flux.yaml"),
        )
        assert (result.passed, result.failed) == (0, 0), result.stdout


if __name__ == "__main__":
    pytest_bazel.main()
