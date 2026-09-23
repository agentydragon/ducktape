"""Typed cdk8s synthesis for source-watcher ArtifactGenerator resources.

The entry point builds each consumer's artifact before its Kustomization node, which
reads `sourceRef` and `path` off it, and passes every artifact here last.
"""

from collections.abc import Sequence
from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    Kustomization,
    KustomizationSpecDependsOn,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)
from source_watcher_crds.io.fluxcd.extensions.source import (
    ArtifactGenerator,
    ArtifactGeneratorSpec,
    ArtifactGeneratorSpecArtifacts,
    ArtifactGeneratorSpecArtifactsCopy,
    ArtifactGeneratorSpecSources,
    ArtifactGeneratorSpecSourcesKind,
)

from cluster.cdk8s.flux import NAMESPACE, flux_kustomization

_ARTIFACT_GENERATORS_DIR = "cluster/k8s/artifact-generators"
_DUCKTAPE_SOURCE = ArtifactGeneratorSpecSources(
    alias="repo", kind=ArtifactGeneratorSpecSourcesKind.GIT_REPOSITORY, name="ducktape", namespace=NAMESPACE
)
_FLUX_SYSTEM_SOURCE = ArtifactGeneratorSpecSources(
    alias="repo", kind=ArtifactGeneratorSpecSourcesKind.GIT_REPOSITORY, name="flux-system", namespace="flux-system"
)


def artifact(name: str, *directories: str) -> ArtifactGeneratorSpecArtifacts:
    """An artifact packaging `directories`, in copy order. The first is the consumer's
    Kustomization directory; following ones are shared bases its Kustomization references."""
    return ArtifactGeneratorSpecArtifacts(
        name=name,
        origin_revision="@repo",
        copy=[
            ArtifactGeneratorSpecArtifactsCopy(from_=f"@repo/{directory}/**", to=f"@artifact/{directory}/")
            for directory in directories
        ],
    )


def artifact_generators(flux_chart: Chart) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        "artifact-generators",
        KustomizationSpecSourceRef(
            kind=KustomizationSpecSourceRefKind.GIT_REPOSITORY, name="ducktape", namespace=NAMESPACE
        ),
        path=f"./{_ARTIFACT_GENERATORS_DIR}",
        depends_on=[KustomizationSpecDependsOn(name="flux-system", namespace="flux-system")],
    )


def write_artifact_generators(
    root: Path,
    *,
    ducktape: Sequence[ArtifactGeneratorSpecArtifacts],
    flux_system: Sequence[ArtifactGeneratorSpecArtifacts],
) -> None:
    """Synthesize one ArtifactGenerator per source repository from the artifacts its consumers read."""
    out_dir = root / _ARTIFACT_GENERATORS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    app = App(outdir=str(out_dir))
    chart = Chart(app, "artifact-generators", disable_resource_name_hashes=True)
    for name, artifacts, source in (
        ("ducktape-artifacts", ducktape, _DUCKTAPE_SOURCE),
        ("flux-system-artifacts", flux_system, _FLUX_SYSTEM_SOURCE),
    ):
        ArtifactGenerator(
            chart,
            name,
            metadata=ApiObjectMetadata(name=name, namespace=NAMESPACE),
            spec=ArtifactGeneratorSpec(artifacts=list(artifacts), sources=[source]),
        )
    app.synth()
