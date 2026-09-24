"""Tests for Flux domain parsing."""

from __future__ import annotations

from pathlib import Path

import pytest_bazel

from cluster.validation.flux import parse_flux_kustomizations
from cluster.validation.flux_bootstrap_auth import check_flux_bootstrap_auth
from util.bazel.runfiles import get_required_path


class TestParseFluxKustomization:
    def test_parses_valid_kustomization(self) -> None:
        kust_file = get_required_path("_main/cluster/validation/testdata/valid/flux-kustomization.yaml")
        kustomizations = parse_flux_kustomizations(kust_file)
        assert len(kustomizations) == 1
        spec = kustomizations["test-app"]
        assert len(spec.depends_on) == 1
        assert spec.depends_on[0].name == "external-secrets-config"

    def test_parses_parked_marker(self, tmp_path: Path) -> None:
        flux_file = tmp_path / "flux-kustomization.yaml"
        _write_yaml(
            flux_file,
            """
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: parked-app
  annotations:
    ducktape.org/parked: "true"
spec:
  path: ./cluster/k8s/parked-app
""",
        )

        spec = parse_flux_kustomizations(flux_file)["parked-app"]

        assert spec.parked


def _write_yaml(path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_bootstrap_gitrepository_cannot_depend_on_sops_managed_auth(tmp_path: Path) -> None:
    """source-controller needs the bootstrap source before Flux can decrypt SOPS resources."""
    _write_yaml(
        tmp_path / "flux" / "flux-system" / "gotk-sync.yaml",
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

    errors = check_flux_bootstrap_auth(tmp_path)
    assert any("sets provider=github" in error for error in errors)
    assert any("references SOPS-managed Secret" in error for error in errors)


if __name__ == "__main__":
    pytest_bazel.main()
