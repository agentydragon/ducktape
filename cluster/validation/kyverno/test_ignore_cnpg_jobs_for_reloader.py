"""Tests for excluding CloudNativePG Jobs from Reloader."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.kyverno.apply import apply_policy
from cluster.validation.kyverno.paths import manifest, policy


@pytest.fixture
def cnpg_jobs_policy() -> Path:
    return policy("ignore-cnpg-jobs-for-reloader.yaml")


class TestIgnoreCnpgJobsForReloader:
    def test_cnpg_job_is_opted_out(self, cnpg_jobs_policy: Path):
        result = apply_policy(cnpg_jobs_policy, manifest("job_cnpg_initdb.yaml"))
        assert result.ok, result.stdout
        assert result.passed == 1, result.stdout
        assert result.mutated_resources[0]["metadata"]["annotations"] == {"reloader.stakater.com/auto": "false"}

    def test_unrelated_job_is_untouched(self, cnpg_jobs_policy: Path):
        resource_path = manifest("job_unrelated.yaml")
        result = apply_policy(cnpg_jobs_policy, resource_path)
        assert result.ok, result.stdout
        assert result.passed == 0, result.stdout
        assert result.mutated_resources[0]["metadata"] == {
            "name": "unrelated-job",
            "namespace": "default",
            "labels": {"app.kubernetes.io/managed-by": "helm"},
        }, result.stdout


if __name__ == "__main__":
    pytest_bazel.main()
