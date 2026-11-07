"""Tests for Flux domain parsing."""

from __future__ import annotations

from pathlib import Path

import durationpy
import pytest
import pytest_bazel

from cluster.validation.flux import FluxReconcileHistoryEntry, parse_flux_kustomizations
from cluster.validation.flux_bootstrap_auth import check_flux_bootstrap_auth


class TestParseFluxKustomization:
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


def test_history_duration_uses_go_duration_syntax() -> None:
    for duration, seconds in [("1h30m1.25s", 5401.25), ("940.5ms", 0.9405), ("-1s", -1.0), ("0", 0.0), (None, 0.0)]:
        entry = FluxReconcileHistoryEntry(last_reconciled_duration=duration)
        assert entry.duration_s == seconds


def test_history_invalid_duration_is_reported() -> None:
    entry = FluxReconcileHistoryEntry(last_reconciled_duration="invalid 2s")
    with pytest.raises(durationpy.DurationError):
        _ = entry.duration_s


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
