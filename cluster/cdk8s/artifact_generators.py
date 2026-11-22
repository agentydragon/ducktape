"""Typed cdk8s synthesis for source-watcher ArtifactGenerator resources.

The entry point builds each consumer's artifact before its Kustomization node, which
reads `sourceRef` and `path` off it, and passes every artifact here last.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecDependsOn
from source_watcher_crds.io.fluxcd.extensions.source import (
    ArtifactGenerator,
    ArtifactGeneratorSpec,
    ArtifactGeneratorSpecArtifacts,
    ArtifactGeneratorSpecArtifactsCopy,
    ArtifactGeneratorSpecSources,
    ArtifactGeneratorSpecSourcesKind,
)

from cluster.cdk8s.flux import DUCKTAPE_SOURCE, NAMESPACE, Kustomization, flux_kustomization, literal_artifact_path
from cluster.cdk8s.generation import write_app
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

_ARTIFACT_GENERATORS_DIR = f"{GENERATED_ROOT}/artifact-generators"
_DUCKTAPE_ARTIFACT_SOURCE = ArtifactGeneratorSpecSources(
    alias="repo",
    kind=ArtifactGeneratorSpecSourcesKind.GIT_REPOSITORY,
    name=DUCKTAPE_SOURCE.name,
    namespace=DUCKTAPE_SOURCE.namespace,
)
_FLUX_SYSTEM_SOURCE = ArtifactGeneratorSpecSources(
    alias="repo", kind=ArtifactGeneratorSpecSourcesKind.GIT_REPOSITORY, name="flux-system", namespace="flux-system"
)


def artifact(
    name: str, directory: str, *bases: str, files: Mapping[str, str] | None = None
) -> ArtifactGeneratorSpecArtifacts:
    """Package the consumer directory and its shared bases at their repository paths.
    Supplemental files map repository sources to destinations relative to the consumer directory."""
    directories = (directory, *bases)
    if any(not literal_artifact_path(path) for path in directories):
        raise ValueError(f"{name=}: artifact directory paths must be relative literal paths: {directories=}")

    copies = [
        ArtifactGeneratorSpecArtifactsCopy(from_=f"@repo/{path}/**", to=f"@artifact/{path}/") for path in directories
    ]
    for source, destination in (files or {}).items():
        if not literal_artifact_path(source) or not literal_artifact_path(destination):
            raise ValueError(f"{name=}: artifact file paths must be relative literal paths: {source=} {destination=}")
        copies.append(
            ArtifactGeneratorSpecArtifactsCopy(from_=f"@repo/{source}", to=f"@artifact/{directory}/{destination}")
        )
    return ArtifactGeneratorSpecArtifacts(name=name, origin_revision="@repo", copy=copies)


def artifact_generators(flux_chart: Chart) -> Kustomization:
    return flux_kustomization(
        flux_chart,
        "artifact-generators",
        DUCKTAPE_SOURCE,
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
    app = App()
    chart = Chart(app, "artifact-generators", disable_resource_name_hashes=True)
    for name, artifacts, source in (
        ("ducktape-artifacts", ducktape, _DUCKTAPE_ARTIFACT_SOURCE),
        ("flux-system-artifacts", flux_system, _FLUX_SYSTEM_SOURCE),
    ):
        ArtifactGenerator(
            chart,
            name,
            metadata=ApiObjectMetadata(name=name, namespace=NAMESPACE),
            spec=ArtifactGeneratorSpec(artifacts=list(artifacts), sources=[source]),
        )
    write_app(root, _ARTIFACT_GENERATORS_DIR, app)
