"""Typed cdk8s synthesis for source-watcher ArtifactGenerator resources.

The entry point builds each consumer's artifact before its Kustomization node, which
reads `sourceRef` and `path` off it, and passes every artifact here last.
"""

from collections.abc import Mapping, Sequence
from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
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

from cluster.cdk8s.flux import NAMESPACE, Kustomization, flux_kustomization
from cluster.cdk8s.generation import write_app
from cluster.cdk8s.manifest_roots import GENERATED_ROOT

_ARTIFACT_GENERATORS_DIR = f"{GENERATED_ROOT}/artifact-generators"
_DUCKTAPE_SOURCE = ArtifactGeneratorSpecSources(
    alias="repo", kind=ArtifactGeneratorSpecSourcesKind.GIT_REPOSITORY, name="ducktape", namespace=NAMESPACE
)
_FLUX_SYSTEM_SOURCE = ArtifactGeneratorSpecSources(
    alias="repo", kind=ArtifactGeneratorSpecSourcesKind.GIT_REPOSITORY, name="flux-system", namespace="flux-system"
)


def artifact(name: str, *directories: str, files: Mapping[str, str] | None = None) -> ArtifactGeneratorSpecArtifacts:
    """An artifact packaging `directories`, in copy order. The first is the consumer's
    Kustomization directory; following ones are shared bases its Kustomization references.
    `files` maps exact repository file paths to paths inside the first directory. These files
    are added only to the generated source artifact, not copied into the repository tree."""

    def valid_path(path: str) -> bool:
        return (
            bool(path)
            and not path.startswith("/")
            and not any(
                part in {"", ".", ".."} or any(character in part for character in "*?[]{}") for part in path.split("/")
            )
        )

    if any(not valid_path(directory) for directory in directories):
        raise ValueError(f"{name=}: artifact directory paths must be relative literal paths: {directories=}")

    copies = [
        ArtifactGeneratorSpecArtifactsCopy(from_=f"@repo/{directory}/**", to=f"@artifact/{directory}/")
        for directory in directories
    ]
    if files:
        if not directories:
            raise ValueError(f"{name=}: file copies need a first artifact directory")
        root = directories[0]
        for source, destination in files.items():
            if not valid_path(source):
                raise ValueError(f"{name=}: repository file path must stay relative: {source=}")
            if not valid_path(destination) or not destination.startswith(f"{root}/"):
                raise ValueError(f"{name=}: file destination must be inside {root}: {destination=}")
            copies.append(ArtifactGeneratorSpecArtifactsCopy(from_=f"@repo/{source}", to=f"@artifact/{destination}"))
    return ArtifactGeneratorSpecArtifacts(name=name, origin_revision="@repo", copy=copies)


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
    app = App()
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
    write_app(root, _ARTIFACT_GENERATORS_DIR, app)
