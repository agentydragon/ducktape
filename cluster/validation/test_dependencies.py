"""Unit tests for the Flux dependency-graph checks."""

from __future__ import annotations

import pytest
import pytest_bazel

from cluster.validation.cluster import ParsedCluster
from cluster.validation.dependencies import check_source_references, validate_dependencies
from cluster.validation.flux import DependsOn, FluxKustomizationSpec, SourceRef
from cluster.validation.k8s import K8sResource


class TestValidateDependencies:
    @pytest.mark.parametrize("depends_on_provider", [False, True])
    def test_certificate_still_requires_its_provider(self, depends_on_provider: bool) -> None:
        cluster = ParsedCluster(
            flux_kustomizations={
                "test-app": FluxKustomizationSpec(
                    depends_on=[DependsOn(name="cert-manager")] if depends_on_provider else []
                ),
                "cert-manager": FluxKustomizationSpec(),
            },
            rendered_resources={"test-app": [K8sResource(kind="Certificate", apiVersion="cert-manager.io/v1")]},
        )
        errors = validate_dependencies(cluster)
        assert errors == (
            []
            if depends_on_provider
            else ["test-app uses Certificate resources but doesn't transitively depend on cert-manager"]
        )

    @pytest.mark.parametrize(
        ("api_version", "provider"),
        [
            ("agentplane.allegedly.works/v1alpha1", "agentplane-crds"),
            ("extensions.agents.x-k8s.io/v1beta1", "agent-sandbox-controller"),
        ],
    )
    def test_a_shared_kind_requires_its_groups_provider(self, api_version: str, provider: str) -> None:
        cluster = ParsedCluster(
            flux_kustomizations={
                "test-app": FluxKustomizationSpec(depends_on=[DependsOn(name="agentplane-crds")]),
                "agentplane-crds": FluxKustomizationSpec(),
                "agent-sandbox-controller": FluxKustomizationSpec(),
            },
            rendered_resources={"test-app": [K8sResource(kind="SandboxTemplate", apiVersion=api_version)]},
        )
        errors = validate_dependencies(cluster)
        assert errors == (
            []
            if provider == "agentplane-crds"
            else ["test-app uses SandboxTemplate resources but doesn't transitively depend on agent-sandbox-controller"]
        )


def test_git_source_refs_resolve_by_kind_name_and_namespace() -> None:
    repo = SourceRef(kind="GitRepository", name="config")
    cluster = ParsedCluster(
        flux_sources={("GitRepository", "sources", "config"), ("HelmRepository", "sources", "charts")},
        flux_kustomizations={
            "local": FluxKustomizationSpec(namespace="sources", source_ref=repo),
            "missing-namespace": FluxKustomizationSpec(namespace="apps", source_ref=repo),
            "remote": FluxKustomizationSpec(
                namespace="apps", source_ref=SourceRef(kind="GitRepository", name="config", namespace="sources")
            ),
            # A GitRepository absent from the repo is outside the validator's view,
            # even if a HelmRepository has that name elsewhere.
            "external": FluxKustomizationSpec(
                namespace="apps", source_ref=SourceRef(kind="GitRepository", name="charts")
            ),
        },
    )
    (error,) = check_source_references(cluster)
    assert "missing-namespace" in error
    assert "sourceRef 'config' resolves to ns=apps" in error


def test_external_artifact_requires_a_declared_output_in_the_target_namespace() -> None:
    cluster = ParsedCluster(
        flux_sources={
            ("ExternalArtifact", "apps", "cert-manager"),
            ("ExternalArtifact", "elsewhere", "ducktape"),
            ("GitRepository", "apps", "ducktape"),
        },
        flux_kustomizations={
            name: FluxKustomizationSpec(namespace="apps", source_ref=SourceRef(kind="ExternalArtifact", name=name))
            for name in ("cert-manager", "ducktape")
        },
    )
    (error,) = check_source_references(cluster)
    assert "sourceRef ExternalArtifact 'ducktape'" in error
    assert "no ArtifactGenerator declares that artifact there" in error


if __name__ == "__main__":
    pytest_bazel.main()
