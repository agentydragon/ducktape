"""Typed cdk8s synthesis for source-watcher ArtifactGenerator resources.

The entry point builds the two resources early. Each active consumer adds its artifact to the
real resource before constructing the Kustomization that reads it; parked artifacts can still
use `artifact()` without being added to either generated resource.
"""

from collections.abc import Mapping
from pathlib import Path

from cdk8s import ApiObjectMetadata, App, Chart, JsonPatch
from flux_kustomize.io.fluxcd.toolkit.kustomize import (
    KustomizationSpecDependsOn,
    KustomizationSpecSourceRef,
    KustomizationSpecSourceRefKind,
)
from source_watcher_crds.io.fluxcd.extensions.source import (
    ArtifactGenerator as _ArtifactGenerator,
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


class ArtifactGenerator(_ArtifactGenerator):
    """Incrementally append typed artifact specs to this source-watcher resource."""

    def add_artifact(
        self, name: str, *directories: str, files: Mapping[str, str] | None = None
    ) -> ArtifactGeneratorSpecArtifacts:
        spec = artifact(name, *directories, files=files)
        self.add_json_patch(JsonPatch.add("/spec/artifacts/-", spec))
        return spec


def artifact_generator_resources(chart: Chart) -> tuple[ArtifactGenerator, ArtifactGenerator]:
    """Build the two source-watcher resources that accept artifacts from their consumers."""
    ducktape = ArtifactGenerator(
        chart,
        "ducktape-artifacts",
        metadata=ApiObjectMetadata(name="ducktape-artifacts", namespace=NAMESPACE),
        spec=ArtifactGeneratorSpec(artifacts=[], sources=[_DUCKTAPE_SOURCE]),
    )
    flux_system = ArtifactGenerator(
        chart,
        "flux-system-artifacts",
        metadata=ApiObjectMetadata(name="flux-system-artifacts", namespace=NAMESPACE),
        spec=ArtifactGeneratorSpec(artifacts=[], sources=[_FLUX_SYSTEM_SOURCE]),
    )
    return ducktape, flux_system


def write_artifact_generators(root: Path, app: App) -> None:
    """Write the source-watcher resources already populated by their consumers."""
    write_app(root, _ARTIFACT_GENERATORS_DIR, app)
