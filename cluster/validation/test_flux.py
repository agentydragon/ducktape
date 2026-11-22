"""Flux domain parsing and cold-start source authentication."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import pytest_bazel
import yaml
from python.runfiles import Runfiles

from cluster.validation.flux import FluxReconcileHistoryEntry, parse_flux_kustomizations


class TestParseFluxKustomization:
    def test_parses_parked_marker_and_target_namespace(self, tmp_path: Path) -> None:
        flux_file = tmp_path / "flux-kustomization.yaml"
        flux_file.write_text(
            """
apiVersion: kustomize.toolkit.fluxcd.io/v1
kind: Kustomization
metadata:
  name: parked-app
  annotations:
    ducktape.org/parked: "true"
spec:
  path: ./cluster/k8s/parked-app
  targetNamespace: app
"""
        )

        spec = parse_flux_kustomizations(flux_file)["parked-app"]

        assert spec.parked
        assert spec.target_namespace == "app"


def test_history_duration_is_seconds_and_missing_is_zero() -> None:
    assert FluxReconcileHistoryEntry(last_reconciled_duration="1h30m1.25s").duration_s == 5401.25
    assert FluxReconcileHistoryEntry().duration_s == 0.0


def test_bootstrap_source_fetches_anonymously(runfiles: Runfiles) -> None:
    """Terraform waits for this source before Flux can decrypt any Git credentials."""
    path = runfiles.root() / "ducktape/cluster/k8s/flux/flux-system/gotk-sync.yaml"
    [source] = [
        doc["spec"]
        for doc in yaml.safe_load_all(path.read_text())
        if isinstance(doc, dict)
        and doc.get("kind") == "GitRepository"
        and doc["metadata"]["name"] == "flux-system"
        and doc["metadata"]["namespace"] == "flux-system"
    ]
    url = urlsplit(source["url"])
    assert url.scheme == "https"
    assert url.username is None
    assert source.get("provider", "generic") == "generic"
    assert "secretRef" not in source


if __name__ == "__main__":
    pytest_bazel.main()
