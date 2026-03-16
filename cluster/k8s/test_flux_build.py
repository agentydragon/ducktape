"""Test: flux build kustomization produces expected resources.

Build failures are caught by the genrule itself (exits non-zero).
This test validates the content of the output.
"""

from __future__ import annotations

import pytest_bazel
import yaml

from cluster.validation.k8s import parse_k8s_resources


def test_flux_build_has_expected_kinds(flux_build_output: str) -> None:
    kinds = {r.kind for r in parse_k8s_resources(yaml.safe_load_all(flux_build_output))}
    assert "Kustomization" in kinds, "No Flux Kustomization resources in flux build output"
    assert "GitRepository" in kinds, "No GitRepository resource in flux build output"


if __name__ == "__main__":
    pytest_bazel.main()
