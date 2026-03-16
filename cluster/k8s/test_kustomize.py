"""Tests that require kustomize build output.

All kustomize-dependent checks are combined in one test target so the
genrule runs once. Individual checks are separate test functions for
clear failure reporting.

Note: kustomize build failures are caught by the genrule itself (exits non-zero),
so there's no need for a separate "all kustomizations build" test.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import pytest
import pytest_bazel
from pydantic import TypeAdapter

from cluster.validation.crd_layering import check_crd_layering
from cluster.validation.kustomize import KustomizeBuildResult
from util.bazel.runfiles import get_required_path

_RESULTS = TypeAdapter(list[KustomizeBuildResult]).validate_json(
    get_required_path("_main/cluster/validation/kustomize_build_results.json").read_bytes()
)


def test_no_duplicate_helmreleases() -> None:
    """Each HelmRelease name must appear in exactly one kustomization."""
    locations: dict[str, list[Path]] = defaultdict(list)
    for result in _RESULTS:
        for resource in result.resources:
            if resource.kind == "HelmRelease":
                locations[resource.name].append(result.kustomization_path.parent)

    duplicates = {name: paths for name, paths in locations.items() if len(paths) > 1}
    assert not duplicates, "Duplicate HelmReleases:\n" + "\n".join(
        f"  {name}: {', '.join(str(p) for p in paths)}" for name, paths in duplicates.items()
    )


@pytest.mark.parametrize("result", _RESULTS, ids=lambda r: str(r.kustomization_path.parent))
def test_no_crd_layering_violations(result: KustomizeBuildResult) -> None:
    """HelmReleases must not be mixed with CRD instances in one kustomization."""
    check_crd_layering(result)


if __name__ == "__main__":
    pytest_bazel.main()
