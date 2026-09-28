"""The two repo-relative trees Flux Kustomization directories live in.

A directory every file of which the generator writes lives under `GENERATED_ROOT`;
`//cluster/cdk8s:test_generate_manifests` holds that tree closed (a file there the generator
does not write fails it). A directory holding any hand-written file (a SOPS secret, an
`image-pins` component, a hand-written `kustomization.yaml`) lives under
`HAND_WRITTEN_ROOT`, generated files beside the hand-written ones. A Kustomization's
directory is never split across the two.

Dependency-free so `cluster/validation` can walk both without loading cdk8s.
"""

from collections.abc import Iterator
from pathlib import Path

GENERATED_ROOT = "cluster/generated"
HAND_WRITTEN_ROOT = "cluster/k8s"
MANIFEST_ROOTS = (HAND_WRITTEN_ROOT, GENERATED_ROOT)


def manifest_files(repo_root: Path, pattern: str = "*.yaml") -> Iterator[Path]:
    """Every file matching `pattern` under both roots of the checkout at `repo_root`."""
    for root in MANIFEST_ROOTS:
        yield from (repo_root / root).rglob(pattern)
