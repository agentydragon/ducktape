"""Unit tests for the Flux dependency-graph checks."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.cluster import ParsedCluster
from cluster.validation.dependencies import (
    check_source_references,
    validate_dependencies,
    validate_operator_dependencies,
)
from cluster.validation.flux import DependsOn, FluxKustomizationSpec, SourceRef
from cluster.validation.k8s import K8sResource
from cluster.validation.kustomize import KustomizeBuildResult


def _build_result(repo_root: Path, subdir: str, resources: list[tuple[str, str]]) -> KustomizeBuildResult:
    """Build a KustomizeBuildResult for a kustomization at repo_root/cluster/k8s/subdir/."""
    return KustomizeBuildResult(
        kustomization_path=repo_root / "cluster/k8s" / subdir / "kustomization.yaml",
        resources=[K8sResource(kind=kind, apiVersion=api) for kind, api in resources],
    )


def _cluster(
    flux_kustomizations: dict[str, FluxKustomizationSpec],
    build_results: list[KustomizeBuildResult] | None = None,
    flux_sources: set[tuple[str, str, str]] | None = None,
) -> ParsedCluster:
    return ParsedCluster(
        flux_kustomizations=flux_kustomizations, build_results=build_results or [], flux_sources=flux_sources or set()
    )


class TestValidateDependencies:
    @pytest.mark.parametrize("depends_on_provider", [False, True])
    def test_certificate_still_requires_its_provider(self, tmp_path: Path, depends_on_provider: bool) -> None:
        cluster = _cluster(
            {
                "test-app": FluxKustomizationSpec(
                    path="./cluster/k8s/test-app",
                    depends_on=[DependsOn(name="cert-manager")] if depends_on_provider else [],
                ),
                "cert-manager": FluxKustomizationSpec(),
            },
            build_results=[_build_result(tmp_path, "test-app", [("Certificate", "cert-manager.io/v1")])],
        )
        errors = validate_dependencies(cluster, tmp_path)
        assert errors == (
            []
            if depends_on_provider
            else ["test-app uses Certificate resources but doesn't transitively depend on cert-manager"]
        )


class TestValidateOperatorDependencies:
    """Tests for validate_operator_dependencies."""

    def test_direct_dep_passes(self, tmp_path: Path) -> None:
        """Kustomization with direct dep on operator passes."""
        cluster = _cluster(
            {
                "my-app": FluxKustomizationSpec(
                    path="./cluster/k8s/my-app", depends_on=[DependsOn(name="some-operator")]
                ),
                "some-operator": FluxKustomizationSpec(path="./cluster/k8s/some-operator"),
            },
            build_results=[_build_result(tmp_path, "my-app", [("MyCRD", "example.com/v1")])],
        )
        assert validate_operator_dependencies(cluster, tmp_path, {"MyCRD": "some-operator"}) == []

    def test_transitive_dep_passes(self, tmp_path: Path) -> None:
        """Transitive dependency (app -> middle -> operator) is accepted."""
        cluster = _cluster(
            {
                "my-app": FluxKustomizationSpec(path="./cluster/k8s/my-app", depends_on=[DependsOn(name="middle")]),
                "middle": FluxKustomizationSpec(
                    path="./cluster/k8s/middle", depends_on=[DependsOn(name="some-operator")]
                ),
                "some-operator": FluxKustomizationSpec(path="./cluster/k8s/some-operator"),
            },
            build_results=[_build_result(tmp_path, "my-app", [("MyCRD", "example.com/v1")])],
        )
        errors = validate_operator_dependencies(cluster, tmp_path, {"MyCRD": "some-operator"})
        assert errors == [], f"Unexpected errors for transitive dep: {errors}"

    def test_missing_dep_fails(self, tmp_path: Path) -> None:
        """Kustomization with no path to operator is flagged."""
        cluster = _cluster(
            {
                "my-app": FluxKustomizationSpec(path="./cluster/k8s/my-app", depends_on=[DependsOn(name="unrelated")]),
                "some-operator": FluxKustomizationSpec(path="./cluster/k8s/some-operator"),
                "unrelated": FluxKustomizationSpec(path="./cluster/k8s/unrelated"),
            },
            build_results=[_build_result(tmp_path, "my-app", [("MyCRD", "example.com/v1")])],
        )
        errors = validate_operator_dependencies(cluster, tmp_path, {"MyCRD": "some-operator"})
        assert any("my-app" in e and "some-operator" in e for e in errors)


class TestSourceReferences:
    """A bare sourceRef resolves in the consumer's own namespace (the PR #3759 outage class);
    an ExternalArtifact sourceRef resolves only to an artifact an ArtifactGenerator declares."""

    def test_source_ref_cross_namespace_fails(self) -> None:
        """Bare sourceRef to a source that exists only in another namespace is flagged."""
        cluster = _cluster(
            {
                "app": FluxKustomizationSpec(
                    namespace="ducktape-flux", source_ref=SourceRef(kind="GitRepository", name="flux-system")
                )
            },
            flux_sources={("GitRepository", "flux-system", "flux-system")},
        )
        errors = check_source_references(cluster)
        assert len(errors) == 1
        assert "sourceRef" in errors[0]
        assert "flux-system" in errors[0]

    def test_source_ref_same_namespace_bare_passes(self) -> None:
        """Bare sourceRef within the source's own namespace resolves there."""
        cluster = _cluster(
            {
                "app": FluxKustomizationSpec(
                    namespace="flux-system", source_ref=SourceRef(kind="GitRepository", name="flux-system")
                )
            },
            flux_sources={("GitRepository", "flux-system", "flux-system")},
        )
        assert check_source_references(cluster) == []

    def test_source_ref_name_shared_by_another_kind_is_not_a_collision(self) -> None:
        """A GitRepository ref is judged against GitRepositories only; a HelmRepository elsewhere
        that happens to share the name is not the source it fails to find."""
        cluster = _cluster(
            {
                "app": FluxKustomizationSpec(
                    namespace="ducktape-flux", source_ref=SourceRef(kind="GitRepository", name="kyverno")
                )
            },
            flux_sources={("HelmRepository", "kyverno", "kyverno")},
        )
        assert check_source_references(cluster) == []

    def test_external_artifact_ref_needs_a_declaring_generator(self) -> None:
        """`kind: ExternalArtifact, name: ducktape` with only a GitRepository of that name (the #6297
        outage class): no generator declares the artifact, so the ref stalls."""
        cluster = _cluster(
            {
                "cert-manager": FluxKustomizationSpec(
                    namespace="ducktape-flux",
                    path="./cluster/k8s/cert-manager/app",
                    source_ref=SourceRef(kind="ExternalArtifact", name="ducktape", namespace="ducktape-flux"),
                )
            },
            flux_sources={
                ("GitRepository", "ducktape-flux", "ducktape"),
                ("ExternalArtifact", "ducktape-flux", "cert-manager"),
            },
        )
        errors = check_source_references(cluster)
        assert len(errors) == 1
        assert "ArtifactGenerator" in errors[0]
        assert "cert-manager" in errors[0]

    def test_external_artifact_ref_resolves_to_declared_artifact(self) -> None:
        """The consumer names an artifact its generator declares."""
        cluster = _cluster(
            {
                "cert-manager": FluxKustomizationSpec(
                    namespace="ducktape-flux",
                    path="./cluster/k8s/cert-manager/app",
                    source_ref=SourceRef(kind="ExternalArtifact", name="cert-manager"),
                )
            },
            flux_sources={("ExternalArtifact", "ducktape-flux", "cert-manager")},
        )
        assert check_source_references(cluster) == []


if __name__ == "__main__":
    pytest_bazel.main()
