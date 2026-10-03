"""The repo-relative trees local Flux Kustomization directories live in.

A directory every file of which the cluster generator writes lives under `GENERATED_ROOT`;
`//cluster/cdk8s:test_generate_manifests` holds that tree closed (a file there the generator
does not write fails it). A directory holding any hand-written file (a SOPS secret, an
`image-pins` component, a hand-written `kustomization.yaml`) lives under
`HAND_WRITTEN_ROOT`, generated files beside the hand-written ones. A Kustomization's
directory is never split across the two; one whose only hand-written file would be its
`image-pins` Component lives under `GENERATED_ROOT` and includes the Component, a directory
of its own under `HAND_WRITTEN_ROOT`, across the roots (cluster/docs/cdk8s_remainder.md
§ Mixed-directory layout). `AGENTPLANE_CRDS_ROOT` is the authored CRD source directory,
packaged directly as a Flux artifact.

Dependency-free so `cluster/validation` can walk all three without loading cdk8s.
"""

from collections.abc import Iterator
from pathlib import Path

GENERATED_ROOT = "cluster/generated"
HAND_WRITTEN_ROOT = "cluster/k8s"
AGENTPLANE_CRDS_ROOT = "agentplane/crds/manifests"
MANIFEST_ROOTS = (HAND_WRITTEN_ROOT, GENERATED_ROOT, AGENTPLANE_CRDS_ROOT)
# Decommissioned or indefinitely suspended third-party apps, kept for manual revival
# (`cluster/AGENTS.md` § Parked). Not a manifest root: Flux applies nothing here.
PARKED_ROOT = "cluster/parked"


def manifest_files(repo_root: Path, pattern: str = "*.yaml") -> Iterator[Path]:
    """Every file matching `pattern` under a Flux manifest root in `repo_root`."""
    for root in MANIFEST_ROOTS:
        yield from (repo_root / root).rglob(pattern)
