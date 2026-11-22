"""Flux domain parsing and cold-start source authentication."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
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


_BOOTSTRAP_SYNC = "cluster/k8s/flux/flux-system/gotk-sync.yaml"


def _git_repository(path: Path, namespace: str, name: str) -> dict[str, Any]:
    [source] = [
        doc["spec"]
        for doc in yaml.safe_load_all(path.read_text())
        if doc["kind"] == "GitRepository"
        and (doc["metadata"]["namespace"], doc["metadata"]["name"]) == (namespace, name)
    ]
    assert isinstance(source, dict)
    return source


@pytest.mark.parametrize(
    ("namespace", "name", "manifest"),
    [
        ("ducktape-flux", "ducktape", "cluster/k8s/flux/ducktape-flux/ducktape-flux.k8s.yaml"),
        ("flux-system", "flux-system", _BOOTSTRAP_SYNC),
    ],
)
def test_artifact_copies_are_in_the_source_checkout(
    runfiles: Runfiles, namespace: str, name: str, manifest: str
) -> None:
    repo = runfiles.root() / "ducktape"
    source = _git_repository(repo / manifest, namespace, name)
    generators = repo / "cluster/generated/artifact-generators/artifact-generators.k8s.yaml"
    [(generator, alias)] = [
        (doc["spec"], ref["alias"])
        for doc in yaml.safe_load_all(generators.read_text())
        for ref in doc["spec"]["sources"]
        if ref["kind"] == "GitRepository"
        and (ref.get("namespace", doc["metadata"]["namespace"]), ref["name"]) == (namespace, name)
    ]
    for artifact in generator["artifacts"]:
        for copy in artifact["copy"]:
            # The artifact builder supports literal files and whole-directory copies ending in /**.
            path = Path(copy["from"]).relative_to(f"@{alias}")
            assert any(path.is_relative_to(root) for root in source["sparseCheckout"]), (name, artifact["name"], path)


def test_bootstrap_source_fetches_anonymously(runfiles: Runfiles) -> None:
    """Terraform waits for this source before Flux can decrypt any Git credentials."""
    source = _git_repository(runfiles.root() / "ducktape" / _BOOTSTRAP_SYNC, "flux-system", "flux-system")
    url = urlsplit(source["url"])
    assert url.scheme == "https"
    assert url.username is None
    assert source.get("provider", "generic") == "generic"
    assert "secretRef" not in source


if __name__ == "__main__":
    pytest_bazel.main()
