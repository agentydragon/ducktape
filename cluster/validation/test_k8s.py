"""Tests for K8s resource parsing."""

from __future__ import annotations

import pytest_bazel

from cluster.validation.k8s import parse_k8s_resources


class TestParseK8sResources:
    """Tests for K8s resource parsing and filtering."""

    def test_skips_empty_and_non_resource_docs(self) -> None:
        """Filters out empty documents and documents without kind."""
        docs = [
            None,
            {},
            {"apiVersion": "v1", "metadata": {"name": "test"}},
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "real"}},
        ]
        [resource] = parse_k8s_resources(docs)
        assert resource.name == "real"


if __name__ == "__main__":
    pytest_bazel.main()
