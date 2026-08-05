"""Unit tests for dependency graph and rule checking."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.cluster import ParsedCluster
from cluster.validation.dependencies import (
    CyclicDependencyError,
    assert_no_cycles,
    check_cross_namespace_references,
    check_required_dependencies,
    validate_operator_dependencies,
)
from cluster.validation.flux import DependsOn, FluxKustomizationSpec, SourceRef
from cluster.validation.k8s import K8sResource
from cluster.validation.kustomize import KustomizeBuildResult


def _build_result(k8s_dir: Path, subdir: str, resources: list[tuple[str, str]]) -> KustomizeBuildResult:
    """Build a KustomizeBuildResult for a kustomization at k8s_dir/subdir/."""
    return KustomizeBuildResult(
        kustomization_path=k8s_dir / subdir / "kustomization.yaml",
        resources=[K8sResource(kind=kind, apiVersion=api) for kind, api in resources],
    )


def _cluster(
    flux_kustomizations: dict[str, FluxKustomizationSpec],
    build_results: list[KustomizeBuildResult] | None = None,
    flux_sources: set[tuple[str, str]] | None = None,
) -> ParsedCluster:
    return ParsedCluster(
        flux_kustomizations=flux_kustomizations, build_results=build_results or [], flux_sources=flux_sources or set()
    )


class TestDependencyGraph:
    """Tests for dependency graph building and cycle detection."""

    def test_builds_graph_from_kustomizations(self) -> None:
        """Builds correct directed graph: edges run from dependent -> prerequisite."""
        cluster = _cluster(
            {
                "app-a": FluxKustomizationSpec(depends_on=[DependsOn(name="core")]),
                "app-b": FluxKustomizationSpec(depends_on=[DependsOn(name="core"), DependsOn(name="app-a")]),
                "core": FluxKustomizationSpec(),
            }
        )

        assert set(cluster.graph.predecessors("core")) == {"app-a", "app-b"}
        assert set(cluster.graph.predecessors("app-a")) == {"app-b"}

    def test_detects_cycle(self) -> None:
        """Detects circular dependencies."""
        cluster = _cluster(
            {
                "a": FluxKustomizationSpec(depends_on=[DependsOn(name="b")]),
                "b": FluxKustomizationSpec(depends_on=[DependsOn(name="a")]),
            }
        )
        with pytest.raises(CyclicDependencyError, match="a"):
            assert_no_cycles(cluster.graph)

    def test_no_cycle_in_dag(self) -> None:
        """No false positives for valid DAGs."""
        cluster = _cluster(
            {
                "core": FluxKustomizationSpec(),
                "app-a": FluxKustomizationSpec(depends_on=[DependsOn(name="core")]),
                "app-b": FluxKustomizationSpec(depends_on=[DependsOn(name="core"), DependsOn(name="app-a")]),
            }
        )
        assert_no_cycles(cluster.graph)  # should not raise


class TestRequiredDependencies:
    """Tests for required dependency checking."""

    def test_detects_missing_dependency(self) -> None:
        """Detects when authentik is missing cert-manager dependency."""
        cluster = _cluster(
            {
                "authentik": FluxKustomizationSpec(),
                "cert-manager": FluxKustomizationSpec(),
                "gateway": FluxKustomizationSpec(depends_on=[DependsOn(name="cert-manager")]),
            }
        )
        errors = check_required_dependencies(cluster)
        assert any("authentik" in e and "cert-manager" in e for e in errors)

    def test_accepts_valid_dependencies(self) -> None:
        """No errors when required dependencies are present."""
        cluster = _cluster(
            {
                "authentik": FluxKustomizationSpec(
                    depends_on=[DependsOn(name="gateway"), DependsOn(name="cert-manager")]
                ),
                "gateway": FluxKustomizationSpec(depends_on=[DependsOn(name="cert-manager")]),
                "cert-manager": FluxKustomizationSpec(),
            }
        )
        errors = check_required_dependencies(cluster)
        assert not any("authentik" in e for e in errors)

    def test_raises_on_unknown_prerequisite(self) -> None:
        """Raises ValueError when a rule references a kustomization not in the cluster."""
        cluster = _cluster({"authentik": FluxKustomizationSpec()})
        with pytest.raises(ValueError, match="unknown kustomization: cert-manager"):
            check_required_dependencies(cluster)


class TestValidateOperatorDependencies:
    """Tests for validate_operator_dependencies."""

    def test_direct_dep_passes(self, tmp_path: Path) -> None:
        """Kustomization with direct dep on operator passes."""
        k8s_dir = tmp_path / "k8s"
        cluster = _cluster(
            {
                "my-app": FluxKustomizationSpec(
                    path="./cluster/k8s/my-app", depends_on=[DependsOn(name="some-operator")]
                ),
                "some-operator": FluxKustomizationSpec(path="./cluster/k8s/some-operator"),
            },
            build_results=[_build_result(k8s_dir, "my-app", [("MyCRD", "example.com/v1")])],
        )
        assert validate_operator_dependencies(cluster, k8s_dir, {"MyCRD": "some-operator"}) == []

    def test_transitive_dep_passes(self, tmp_path: Path) -> None:
        """Transitive dependency (app -> middle -> operator) is accepted."""
        k8s_dir = tmp_path / "k8s"
        cluster = _cluster(
            {
                "my-app": FluxKustomizationSpec(path="./cluster/k8s/my-app", depends_on=[DependsOn(name="middle")]),
                "middle": FluxKustomizationSpec(
                    path="./cluster/k8s/middle", depends_on=[DependsOn(name="some-operator")]
                ),
                "some-operator": FluxKustomizationSpec(path="./cluster/k8s/some-operator"),
            },
            build_results=[_build_result(k8s_dir, "my-app", [("MyCRD", "example.com/v1")])],
        )
        errors = validate_operator_dependencies(cluster, k8s_dir, {"MyCRD": "some-operator"})
        assert errors == [], f"Unexpected errors for transitive dep: {errors}"

    def test_missing_dep_fails(self, tmp_path: Path) -> None:
        """Kustomization with no path to operator is flagged."""
        k8s_dir = tmp_path / "k8s"
        cluster = _cluster(
            {
                "my-app": FluxKustomizationSpec(path="./cluster/k8s/my-app", depends_on=[DependsOn(name="unrelated")]),
                "some-operator": FluxKustomizationSpec(path="./cluster/k8s/some-operator"),
                "unrelated": FluxKustomizationSpec(path="./cluster/k8s/unrelated"),
            },
            build_results=[_build_result(k8s_dir, "my-app", [("MyCRD", "example.com/v1")])],
        )
        errors = validate_operator_dependencies(cluster, k8s_dir, {"MyCRD": "some-operator"})
        assert any("my-app" in e and "some-operator" in e for e in errors)


class TestCrossNamespaceReferences:
    """Tests for check_cross_namespace_references — the guardrail for the PR #3759 outage class.

    Flux resolves a bare dependsOn/sourceRef (no explicit namespace) in the
    consumer's own namespace; if the target lives elsewhere the reference misses
    and the Kustomization stalls with DependencyNotReady.
    """

    def test_bare_cross_namespace_depends_on_fails(self) -> None:
        """flux-system consumer bare-depending on a CR that only exists in ducktape-flux."""
        cluster = _cluster(
            {
                "agent-machine-access-tf": FluxKustomizationSpec(
                    namespace="flux-system", depends_on=[DependsOn(name="public-coder-agent-namespace")]
                ),
                "public-coder-agent-namespace": FluxKustomizationSpec(namespace="ducktape-flux"),
            }
        )
        errors = check_cross_namespace_references(cluster)
        assert len(errors) == 1
        e = errors[0]
        assert "agent-machine-access-tf" in e
        assert "flux-system" in e
        assert "public-coder-agent-namespace" in e
        assert "ducktape-flux" in e

    def test_qualified_cross_namespace_depends_on_passes(self) -> None:
        """Explicit namespace on the entry resolves correctly across namespaces."""
        cluster = _cluster(
            {
                "agent-machine-access-tf": FluxKustomizationSpec(
                    namespace="flux-system",
                    depends_on=[DependsOn(name="public-coder-agent-namespace", namespace="ducktape-flux")],
                ),
                "public-coder-agent-namespace": FluxKustomizationSpec(namespace="ducktape-flux"),
            }
        )
        assert check_cross_namespace_references(cluster) == []

    def test_same_namespace_bare_depends_on_passes(self) -> None:
        """Bare ref within the same namespace resolves there (the allowed default-namespace case)."""
        cluster = _cluster(
            {
                "public-coder-agent-proxy": FluxKustomizationSpec(
                    namespace="ducktape-flux", depends_on=[DependsOn(name="public-coder-agent-namespace")]
                ),
                "public-coder-agent-namespace": FluxKustomizationSpec(namespace="ducktape-flux"),
            }
        )
        assert check_cross_namespace_references(cluster) == []

    def test_external_depends_on_skipped(self) -> None:
        """A ref to a name absent from this repo (cross-repo, e.g. gaffer-private/augur) is not flagged."""
        cluster = _cluster(
            {"app": FluxKustomizationSpec(namespace="flux-system", depends_on=[DependsOn(name="augur")])}
        )
        assert check_cross_namespace_references(cluster) == []

    def test_source_ref_cross_namespace_fails(self) -> None:
        """Bare sourceRef to a source that exists only in another namespace is flagged."""
        cluster = _cluster(
            {"app": FluxKustomizationSpec(namespace="ducktape-flux", source_ref=SourceRef(name="flux-system"))},
            flux_sources={("flux-system", "flux-system")},
        )
        errors = check_cross_namespace_references(cluster)
        assert len(errors) == 1
        assert "sourceRef" in errors[0]
        assert "flux-system" in errors[0]

    def test_source_ref_same_namespace_bare_passes(self) -> None:
        """Bare sourceRef within the source's own namespace resolves there."""
        cluster = _cluster(
            {"app": FluxKustomizationSpec(namespace="flux-system", source_ref=SourceRef(name="flux-system"))},
            flux_sources={("flux-system", "flux-system")},
        )
        assert check_cross_namespace_references(cluster) == []


if __name__ == "__main__":
    pytest_bazel.main()
