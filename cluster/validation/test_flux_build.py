"""Integration test: `flux build` renders the complete Flux kustomization tree.

Separate target from test_cluster_integration: validate_flux_build needs every
manifest the root kustomization references — including the cluster/k8s sub-Bazel-
packages that the `**` glob in //cluster/k8s:cluster_all_files cannot reach (it
stops at package boundaries). Those sub-package manifests are pulled in here via
their `:manifests` filegroups. validate_flux_build only shells out to `flux`; it
does not call parse_cluster, so the extra files don't reach the parse-based checks.
"""

from __future__ import annotations

from pathlib import Path

import pytest_bazel

from cluster.validation.flux import validate_flux_build


async def test_flux_build_renders_tree(k8s_dir: Path) -> None:
    """`flux build` of the whole tree succeeds and yields Kustomization + GitRepository resources."""
    errors = await validate_flux_build(k8s_dir)
    assert not errors, "\n".join(errors)


if __name__ == "__main__":
    pytest_bazel.main()
