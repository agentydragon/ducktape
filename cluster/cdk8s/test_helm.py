"""`helm_release` pins a Helm repository's chart to a version and takes none for a Git
repository's, whose ref pins it."""

from collections.abc import Callable
from pathlib import Path

import pytest
import pytest_bazel
from cdk8s import ApiObjectMetadata, Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from constructs import Construct

from cluster.cdk8s.helm import helm_release, https_helm_repository
from cluster.cdk8s.providers.flux.git_repository import GitRepository
from cluster.cdk8s.providers.flux.helm_repository import HelmRepository
from cluster.cdk8s.testing.scratch import scratch_chart


def _helm_repository(scope: Construct) -> HelmRepository:
    return https_helm_repository(scope, "test-charts", "test-namespace", url="https://charts.test.invalid")


def _git_repository(scope: Construct) -> GitRepository:
    return GitRepository(
        scope,
        "source",
        metadata=ApiObjectMetadata(name="test-source", namespace="test-namespace"),
        url="https://git.test.invalid/test-chart.git",
        interval="1h",
    )


@pytest.mark.parametrize(
    ("repository", "version"), [(_helm_repository, None), (_git_repository, "1.0.0")], ids=["helm", "git"]
)
def test_version_mismatched_to_source_raises(
    tmp_path: Path, repository: Callable[[Construct], HelmRepository | GitRepository], version: str | None
) -> None:
    chart = scratch_chart(tmp_path)
    with pytest.raises(ValueError, match="version"):
        helm_release(
            chart,
            "test-release",
            "test-namespace",
            repository=repository(chart),
            chart="test-chart",
            version=version,
            interval="1h",
            values={},
        )


def test_git_repository_chart_references_its_source(tmp_path: Path) -> None:
    chart = scratch_chart(tmp_path)
    helm_release(
        chart,
        "test-release",
        "test-namespace",
        repository=_git_repository(chart),
        chart="charts/test-chart",
        interval="1h",
        values={},
    )
    (_, release) = Cdk8sTesting.synth(chart)
    assert release["spec"]["chart"]["spec"] == {
        "chart": "charts/test-chart",
        "sourceRef": {"kind": "GitRepository", "name": "test-source", "namespace": "test-namespace"},
    }


if __name__ == "__main__":
    pytest_bazel.main()
