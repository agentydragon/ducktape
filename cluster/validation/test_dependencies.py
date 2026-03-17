"""Unit tests for dependency graph and rule checking."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.cluster import ParsedCluster
from cluster.validation.dependencies import (
    CyclicDependencyError,
    assert_no_cycles,
    check_required_dependencies,
    validate_operator_dependencies,
)
from cluster.validation.flux import DependsOn, FluxKustomization, FluxKustomizationSpec
from cluster.validation.k8s import K8sResource


def _cluster(
    flux_kustomizations: dict[str, FluxKustomization], source_resources: dict[Path, list[tuple[str, str]]] | None = None
) -> ParsedCluster:
    return ParsedCluster(
        flux_kustomizations=flux_kustomizations,
        source_resources={
            p: [K8sResource(kind=kind, apiVersion=api) for kind, api in rs]
            for p, rs in (source_resources or {}).items()
        },
    )


class TestDependencyGraph:
    """Tests for dependency graph building and cycle detection."""

    def test_builds_graph_from_kustomizations(self) -> None:
        """Builds correct directed graph: edges run from dependent -> prerequisite."""
        cluster = _cluster(
            {
                "app-a": FluxKustomization(
                    name="app-a",
                    file_path=Path("./k8s/app-a"),
                    spec=FluxKustomizationSpec(depends_on=[DependsOn(name="core")]),
                ),
                "app-b": FluxKustomization(
                    name="app-b",
                    file_path=Path("./k8s/app-b"),
                    spec=FluxKustomizationSpec(depends_on=[DependsOn(name="core"), DependsOn(name="app-a")]),
                ),
                "core": FluxKustomization(name="core", file_path=Path("./k8s/core")),
            }
        )

        assert set(cluster.graph.predecessors("core")) == {"app-a", "app-b"}
        assert set(cluster.graph.predecessors("app-a")) == {"app-b"}

    def test_detects_cycle(self) -> None:
        """Detects circular dependencies."""
        cluster = _cluster(
            {
                "a": FluxKustomization(
                    name="a", file_path=Path("./k8s/a"), spec=FluxKustomizationSpec(depends_on=[DependsOn(name="b")])
                ),
                "b": FluxKustomization(
                    name="b", file_path=Path("./k8s/b"), spec=FluxKustomizationSpec(depends_on=[DependsOn(name="a")])
                ),
            }
        )
        with pytest.raises(CyclicDependencyError, match="a"):
            assert_no_cycles(cluster.graph)

    def test_no_cycle_in_dag(self) -> None:
        """No false positives for valid DAGs."""
        cluster = _cluster(
            {
                "core": FluxKustomization(name="core", file_path=Path("./k8s/core")),
                "app-a": FluxKustomization(
                    name="app-a",
                    file_path=Path("./k8s/app-a"),
                    spec=FluxKustomizationSpec(depends_on=[DependsOn(name="core")]),
                ),
                "app-b": FluxKustomization(
                    name="app-b",
                    file_path=Path("./k8s/app-b"),
                    spec=FluxKustomizationSpec(depends_on=[DependsOn(name="core"), DependsOn(name="app-a")]),
                ),
            }
        )
        assert_no_cycles(cluster.graph)  # should not raise


class TestRequiredDependencies:
    """Tests for required dependency checking."""

    def test_detects_missing_dependency(self) -> None:
        """Detects when authentik is missing cert-manager dependency."""
        cluster = _cluster(
            {
                "authentik": FluxKustomization(name="authentik", file_path=Path("./k8s/authentik")),
                "cert-manager": FluxKustomization(name="cert-manager", file_path=Path("./k8s/cert-manager")),
                "gateway": FluxKustomization(
                    name="gateway",
                    file_path=Path("./k8s/gateway"),
                    spec=FluxKustomizationSpec(depends_on=[DependsOn(name="cert-manager")]),
                ),
            }
        )
        errors = check_required_dependencies(cluster)
        assert any("authentik" in e and "cert-manager" in e for e in errors)

    def test_accepts_valid_dependencies(self) -> None:
        """No errors when required dependencies are present."""
        cluster = _cluster(
            {
                "authentik": FluxKustomization(
                    name="authentik",
                    file_path=Path("./k8s/authentik"),
                    spec=FluxKustomizationSpec(depends_on=[DependsOn(name="gateway"), DependsOn(name="cert-manager")]),
                ),
                "gateway": FluxKustomization(
                    name="gateway",
                    file_path=Path("./k8s/gateway"),
                    spec=FluxKustomizationSpec(depends_on=[DependsOn(name="cert-manager")]),
                ),
                "cert-manager": FluxKustomization(name="cert-manager", file_path=Path("./k8s/cert-manager")),
            }
        )
        errors = check_required_dependencies(cluster)
        assert not any("authentik" in e for e in errors)

    def test_raises_on_unknown_prerequisite(self) -> None:
        """Raises ValueError when a rule references a kustomization not in the cluster."""
        cluster = _cluster({"authentik": FluxKustomization(name="authentik", file_path=Path("./k8s/authentik"))})
        with pytest.raises(ValueError, match="unknown kustomization: cert-manager"):
            check_required_dependencies(cluster)


class TestValidateOperatorDependencies:
    """Tests for validate_operator_dependencies."""

    def test_direct_dep_passes(self, tmp_path: Path) -> None:
        """Kustomization with direct dep on operator passes."""
        k8s_dir = tmp_path / "k8s"
        cluster = _cluster(
            {
                "my-app": FluxKustomization(
                    name="my-app",
                    file_path=k8s_dir / "my-app",
                    spec=FluxKustomizationSpec(depends_on=[DependsOn(name="some-operator")]),
                ),
                "some-operator": FluxKustomization(name="some-operator", file_path=k8s_dir / "some-operator"),
            },
            {k8s_dir / "my-app" / "resource.yaml": [("MyCRD", "example.com/v1")]},
        )
        assert validate_operator_dependencies(cluster, k8s_dir, {"MyCRD": "some-operator"}) == []

    def test_transitive_dep_passes(self, tmp_path: Path) -> None:
        """Transitive dependency (app -> middle -> operator) is accepted."""
        k8s_dir = tmp_path / "k8s"
        cluster = _cluster(
            {
                "my-app": FluxKustomization(
                    name="my-app",
                    file_path=k8s_dir / "my-app",
                    spec=FluxKustomizationSpec(depends_on=[DependsOn(name="middle")]),
                ),
                "middle": FluxKustomization(
                    name="middle",
                    file_path=k8s_dir / "middle",
                    spec=FluxKustomizationSpec(depends_on=[DependsOn(name="some-operator")]),
                ),
                "some-operator": FluxKustomization(name="some-operator", file_path=k8s_dir / "some-operator"),
            },
            {k8s_dir / "my-app" / "resource.yaml": [("MyCRD", "example.com/v1")]},
        )
        errors = validate_operator_dependencies(cluster, k8s_dir, {"MyCRD": "some-operator"})
        assert errors == [], f"Unexpected errors for transitive dep: {errors}"

    def test_missing_dep_fails(self, tmp_path: Path) -> None:
        """Kustomization with no path to operator is flagged."""
        k8s_dir = tmp_path / "k8s"
        cluster = _cluster(
            {
                "my-app": FluxKustomization(
                    name="my-app",
                    file_path=k8s_dir / "my-app",
                    spec=FluxKustomizationSpec(depends_on=[DependsOn(name="unrelated")]),
                ),
                "some-operator": FluxKustomization(name="some-operator", file_path=k8s_dir / "some-operator"),
                "unrelated": FluxKustomization(name="unrelated", file_path=k8s_dir / "unrelated"),
            },
            {k8s_dir / "my-app" / "resource.yaml": [("MyCRD", "example.com/v1")]},
        )
        errors = validate_operator_dependencies(cluster, k8s_dir, {"MyCRD": "some-operator"})
        assert any("my-app" in e and "some-operator" in e for e in errors)


if __name__ == "__main__":
    pytest_bazel.main()
