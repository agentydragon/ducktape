"""Tests for Flux domain parsing."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest_bazel

from cluster.validation.dependencies import build_dependency_graph, find_cycles
from cluster.validation.flux import parse_flux_kustomization
from util.bazel.runfiles import get_required_path


class TestParseFluxKustomization:
    """Tests for parsing flux-kustomization.yaml files."""

    def test_parses_valid_kustomization(self, tmp_path: Path) -> None:
        """Parses flux-kustomization.yaml correctly."""
        kust_file = tmp_path / "flux-kustomization.yaml"
        kust_file.write_text(
            dedent("""
            apiVersion: kustomize.toolkit.fluxcd.io/v1
            kind: Kustomization
            metadata:
              name: test-app
              namespace: flux-system
            spec:
              interval: 10m
              path: ./k8s/test-app
              dependsOn:
                - name: core
        """)
        )
        kustomizations = parse_flux_kustomization(kust_file)
        assert len(kustomizations) == 1
        assert kustomizations[0].name == "test-app"
        assert len(kustomizations[0].spec.depends_on) == 1
        assert kustomizations[0].spec.depends_on[0].name == "core"

    def test_loads_cycle_testdata(self) -> None:
        """Loads cycle testdata and detects the cycle."""
        testdata_dir = get_required_path("_main/cluster/validation/testdata/cycle")

        kustomizations = {}
        for flux_file in testdata_dir.rglob("flux-kustomization.yaml"):
            for kust in parse_flux_kustomization(flux_file):
                kustomizations[kust.name] = kust

        graph = build_dependency_graph(kustomizations)
        all_nodes = set(kustomizations.keys()) | set().union(*graph.values()) if graph else set(kustomizations.keys())
        cycles = find_cycles(graph, all_nodes)

        assert len(cycles) > 0, "Should detect cycle between cycle-a and cycle-b"


if __name__ == "__main__":
    pytest_bazel.main()
