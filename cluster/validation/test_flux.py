"""Tests for Flux domain parsing."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.cluster import ParsedCluster
from cluster.validation.dependencies import CyclicDependencyError, assert_no_cycles
from cluster.validation.flux import check_flux_bootstrap_auth, parse_flux_kustomizations
from cluster.validation.k8s import parse_k8s_resources
from cluster.validation.kustomize import KustomizeBuildResult
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


def _write_yaml(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def _cluster_with_resources(*docs: dict) -> ParsedCluster:
    result = KustomizeBuildResult(
        kustomization_path=Path("cluster/k8s/synthetic/kustomization.yaml"), resources=parse_k8s_resources(docs)
    )
    return ParsedCluster(build_results=[result])


def _git_repository(name: str, *, secret_name: str | None = None) -> dict:
    spec: dict = {"url": "https://github.com/agentydragon/ducktape.git", "ref": {"branch": "devel"}}
    if secret_name:
        spec["provider"] = "github"
        spec["secretRef"] = {"name": secret_name}
    return {
        "apiVersion": "source.toolkit.fluxcd.io/v1",
        "kind": "GitRepository",
        "metadata": {"name": name, "namespace": "flux-system"},
        "spec": spec,
    }


def _image_update_automation(source_name: str) -> dict:
    return {
        "apiVersion": "image.toolkit.fluxcd.io/v1beta2",
        "kind": "ImageUpdateAutomation",
        "metadata": {"name": "all-images", "namespace": "flux-system"},
        "spec": {
            "sourceRef": {"kind": "GitRepository", "name": source_name},
            "git": {
                "checkout": {"ref": {"branch": "devel"}},
                "commit": {"author": {"name": "flux-image-automation", "email": "flux@allegedly.works"}},
                "push": {"branch": "devel"},
            },
            "update": {"strategy": "Setters", "path": "./cluster/k8s"},
        },
    }


def test_bootstrap_gitrepository_cannot_depend_on_sops_managed_auth(tmp_path: Path) -> None:
    """source-controller needs the bootstrap source before Flux can decrypt SOPS resources."""
    _write_yaml(
        tmp_path / "flux-system" / "gotk-sync.yaml",
        """
apiVersion: source.toolkit.fluxcd.io/v1
kind: GitRepository
metadata:
  name: flux-system
  namespace: flux-system
spec:
  interval: 1m
  provider: github
  secretRef:
    name: ducktape-automation-github-app
  url: https://github.com/agentydragon/ducktape.git
""",
    )
    _write_yaml(
        tmp_path / "flux-system" / "ducktape-automation-github-app.sops.yaml",
        """
apiVersion: v1
kind: Secret
metadata:
  name: ducktape-automation-github-app
  namespace: flux-system
type: Opaque
stringData:
  githubAppPrivateKey: ENC[AES256_GCM,data:example]
""",
    )

    errors = check_flux_bootstrap_auth(ParsedCluster(), tmp_path)
    assert any("sets provider=github" in error for error in errors)
    assert any("references SOPS-managed Secret" in error for error in errors)


def test_image_update_automation_requires_authenticated_source(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "flux-system" / "gotk-sync.yaml", "")
    cluster = _cluster_with_resources(_git_repository("flux-system"), _image_update_automation("flux-system"))

    errors = check_flux_bootstrap_auth(cluster, tmp_path)

    assert errors == [
        "ImageUpdateAutomation 'flux-system/all-images' uses GitRepository "
        "'flux-system/flux-system' without a secretRef. Image automation writes commits back to git, so the "
        "referenced source must carry push-capable credentials instead of reusing the anonymous bootstrap source."
    ]


def test_authenticated_image_update_source_is_valid(tmp_path: Path) -> None:
    _write_yaml(tmp_path / "flux-system" / "gotk-sync.yaml", "")
    cluster = _cluster_with_resources(
        _git_repository("ducktape-write", secret_name="ducktape-automation-github-app"),
        _image_update_automation("ducktape-write"),
    )

    assert check_flux_bootstrap_auth(cluster, tmp_path) == []


if __name__ == "__main__":
    pytest_bazel.main()
