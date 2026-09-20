"""Directory packaging and Flux references for source-watcher artifacts."""

from flux_kustomize.io.fluxcd.toolkit.kustomize import KustomizationSpecSourceRef, KustomizationSpecSourceRefKind
from source_watcher_crds.io.fluxcd.extensions.source import (
    ArtifactGeneratorSpecArtifacts,
    ArtifactGeneratorSpecArtifactsCopy,
)

from cluster.cdk8s.flux import NAMESPACE


def directory_artifact(name: str, directory: str, *shared_directories: str) -> ArtifactGeneratorSpecArtifacts:
    return ArtifactGeneratorSpecArtifacts(
        name=name,
        origin_revision="@repo",
        copy=[
            ArtifactGeneratorSpecArtifactsCopy(from_=f"@repo/{path}/**", to=f"@artifact/{path}/")
            for path in (directory, *shared_directories)
        ],
    )


def artifact_source_ref(artifact: ArtifactGeneratorSpecArtifacts) -> KustomizationSpecSourceRef:
    return KustomizationSpecSourceRef(
        kind=KustomizationSpecSourceRefKind.EXTERNAL_ARTIFACT, name=artifact.name, namespace=NAMESPACE
    )
