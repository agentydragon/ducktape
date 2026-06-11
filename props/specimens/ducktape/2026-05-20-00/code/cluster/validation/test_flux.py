"""Tests for Flux domain parsing."""

from __future__ import annotations

import pytest
import pytest_bazel

from cluster.validation.cluster import ParsedCluster
from cluster.validation.dependencies import CyclicDependencyError, assert_no_cycles
from cluster.validation.flux import parse_flux_kustomizations
from util.bazel.runfiles import get_required_path


class TestParseFluxKustomization:
    """Tests for parsing flux-kustomization.yaml files."""

    def test_parses_valid_kustomization(self) -> None:
        """Parses a real flux-kustomization.yaml manifest correctly."""
        kust_file = get_required_path("_main/cluster/validation/testdata/valid/flux-kustomization.yaml")
        kustomizations = parse_flux_kustomizations(kust_file)
        assert len(kustomizations) == 1
        spec = kustomizations["test-app"]
        assert len(spec.depends_on) == 1
        assert spec.depends_on[0].name == "external-secrets-config"

    def test_cycle_manifests_raise(self) -> None:
        """Cycle testdata manifests are detected as a circular dependency."""
        testdata_dir = get_required_path("_main/cluster/validation/testdata/cycle")
        kustomizations = {}
        for flux_file in testdata_dir.rglob("flux-kustomization.yaml"):
            kustomizations.update(parse_flux_kustomizations(flux_file))

        cluster = ParsedCluster(flux_kustomizations=kustomizations)
        with pytest.raises(CyclicDependencyError):
            assert_no_cycles(cluster.graph)


if __name__ == "__main__":
    pytest_bazel.main()
