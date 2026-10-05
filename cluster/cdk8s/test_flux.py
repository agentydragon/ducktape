import pytest
import pytest_bazel
from cdk8s import ApiObjectMetadata, Chart, Testing as Cdk8sTesting  # pytest auto-collects classes named Test*
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    Kustomization,
    KustomizationSpec,
    KustomizationSpecHealthChecks,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)
from source_watcher_crds.io.fluxcd.extensions.source import (
    ArtifactGeneratorSpecArtifacts,
    ArtifactGeneratorSpecArtifactsCopy,
)

from cluster.cdk8s.artifact_generators import artifact
from cluster.cdk8s.flux import (
    SOPS_DECRYPTION,
    RenderedDirectory,
    artifact_directory,
    flux_kustomization,
    kustomizations_chart,
)

_HEALTH_CHECKS = [KustomizationSpecHealthChecks(kind="Deployment", name="test-app", namespace="test-ns")]


def _raw_kustomization(chart: Chart, *, wait: bool, health_checks: list[KustomizationSpecHealthChecks] | None) -> None:
    """Built directly, not through `flux_kustomization`, so its own guard does not run."""
    Kustomization(
        chart,
        "test-app",
        metadata=ApiObjectMetadata(name="test-app"),
        spec=KustomizationSpec(
            source_ref=KustomizationSpecSourceRef(kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="test-repo"),
            path="./test-app",
            interval="10m",
            prune=True,
            wait=wait,
            health_checks=health_checks,
        ),
    )


def test_wait_with_health_checks_fails_synth() -> None:
    chart = kustomizations_chart(Cdk8sTesting.app())
    _raw_kustomization(chart, wait=True, health_checks=_HEALTH_CHECKS)
    with pytest.raises(
        Exception, match=r"(?s)Validation failed.*Kustomization/test-app: wait: true ignores healthChecks"
    ):
        Cdk8sTesting.synth(chart)


@pytest.mark.parametrize(("wait", "health_checks"), [(False, _HEALTH_CHECKS), (True, None)])
def test_wait_or_health_checks_alone_synthesizes(
    wait: bool, health_checks: list[KustomizationSpecHealthChecks] | None
) -> None:
    chart = kustomizations_chart(Cdk8sTesting.app())
    _raw_kustomization(chart, wait=wait, health_checks=health_checks)
    (rendered,) = Cdk8sTesting.synth(chart)
    assert rendered["spec"]["wait"] is wait


def test_rendered_directory_refuses_a_second_decryption() -> None:
    directory = RenderedDirectory(artifact=artifact("test-app", "test/app"), decryption=None)
    with pytest.raises(ValueError, match="derives its decryption"):
        flux_kustomization(kustomizations_chart(Cdk8sTesting.app()), "test-app", directory, decryption=SOPS_DECRYPTION)


def test_artifact_directory_is_the_first_copy_of_an_artifact_with_bases() -> None:
    assert artifact_directory(artifact("test-app", "test/app", "test/base")) == "test/app"


@pytest.mark.parametrize(
    "copy",
    [
        ArtifactGeneratorSpecArtifactsCopy(from_="@repo/test/app/**", to="@artifact/test/other/"),
        ArtifactGeneratorSpecArtifactsCopy(from_="@repo/test/app/*.yaml", to="@artifact/test/app/"),
        ArtifactGeneratorSpecArtifactsCopy(from_="@repo/test/app/**", to="@artifact/test/app/", exclude=["*.md"]),
    ],
)
def test_artifact_directory_refuses_a_partial_or_moved_copy(copy: ArtifactGeneratorSpecArtifactsCopy) -> None:
    # Only the whole-directory guard names the offending `copy.from_`; the "copies nothing" error does not.
    with pytest.raises(ValueError, match=r"copy\.from_="):
        artifact_directory(ArtifactGeneratorSpecArtifacts(name="test-app", origin_revision="@repo", copy=[copy]))


if __name__ == "__main__":
    pytest_bazel.main()
