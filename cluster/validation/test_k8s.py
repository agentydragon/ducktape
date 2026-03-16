"""Tests for K8s resource parsing."""

from __future__ import annotations

import pytest_bazel

from cluster.validation.k8s import parse_k8s_resources


class TestParseK8sResources:
    """Tests for K8s resource parsing and filtering."""

    def test_parses_basic_resource(self) -> None:
        """Parses basic K8s resource."""
        doc = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "test-cm", "namespace": "default"}}
        [resource] = parse_k8s_resources([doc])
        assert resource.kind == "ConfigMap"
        assert resource.api_version == "v1"
        assert resource.name == "test-cm"
        assert resource.namespace == "default"

    def test_parses_helmrelease_chart_version(self) -> None:
        """Parses HelmRelease with chart version."""
        doc = {
            "apiVersion": "helm.toolkit.fluxcd.io/v2",
            "kind": "HelmRelease",
            "metadata": {"name": "test-hr"},
            "spec": {"chart": {"spec": {"version": "1.2.3"}}},
        }
        [resource] = parse_k8s_resources([doc])
        assert resource.kind == "HelmRelease"
        assert resource.chart_version == "1.2.3"

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
