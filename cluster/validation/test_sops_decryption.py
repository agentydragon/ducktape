"""Tests for the SOPS-secret-without-decryption-block check."""

from __future__ import annotations

from pathlib import Path

import pytest_bazel

from cluster.validation.checks import check_sops_decryption_blocks
from cluster.validation.cluster import ParsedCluster
from cluster.validation.flux import Decryption, FluxKustomizationSpec
from cluster.validation.k8s import SecretRef, parse_k8s_resources
from cluster.validation.kustomize import KustomizeBuildResult

_NAME = "synthetic"
_PATH = "./cluster/k8s/synthetic"


def _sops_secret() -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "sops-secret", "namespace": "default"},
        "data": {"password": "ENC[AES256_GCM,data:abc,type:str]"},
        "sops": {"age": [{"recipient": "age1example", "enc": "stub"}], "lastmodified": "2026-01-01T00:00:00Z"},
    }


def _plain_secret() -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "plain-secret", "namespace": "default"},
        "stringData": {"password": "plaintext"},
    }


def _cluster(tmp_path: Path, secret_doc: dict, spec: FluxKustomizationSpec) -> ParsedCluster:
    """Build a ParsedCluster whose single flux kustomization renders `secret_doc`.

    `spec.local_dir(k8s_dir)` must resolve to the build result's kustomization dir:
    path `./cluster/k8s/synthetic` strips the `cluster/k8s/` prefix → `<k8s_dir>/synthetic`,
    so the build result lives at `<tmp_path>/synthetic/kustomization.yaml`."""
    return ParsedCluster(
        flux_kustomizations={_NAME: spec},
        build_results=[
            KustomizeBuildResult(
                kustomization_path=tmp_path / "synthetic" / "kustomization.yaml",
                resources=parse_k8s_resources([secret_doc]),
            )
        ],
    )


def test_sops_secret_without_decryption_is_flagged(tmp_path: Path) -> None:
    cluster = _cluster(tmp_path, _sops_secret(), FluxKustomizationSpec(path=_PATH))

    errors = check_sops_decryption_blocks(cluster, tmp_path)

    assert len(errors) == 1
    assert _NAME in errors[0]
    assert "decryption.provider: sops" in errors[0]


def test_sops_secret_with_decryption_and_secretref_is_ok(tmp_path: Path) -> None:
    spec = FluxKustomizationSpec(
        path=_PATH, decryption=Decryption(provider="sops", secret_ref=SecretRef(name="sops-age-cluster-secrets"))
    )
    cluster = _cluster(tmp_path, _sops_secret(), spec)

    assert check_sops_decryption_blocks(cluster, tmp_path) == []


def test_sops_secret_with_provider_but_no_secretref_is_flagged(tmp_path: Path) -> None:
    # The litellm-keys-tf (#2797) shape: provider: sops declared, but no secretRef —
    # Flux is told to decrypt SOPS yet given no key, so ciphertext is applied literally.
    spec = FluxKustomizationSpec(path=_PATH, decryption=Decryption(provider="sops"))
    cluster = _cluster(tmp_path, _sops_secret(), spec)

    errors = check_sops_decryption_blocks(cluster, tmp_path)

    assert len(errors) == 1
    assert _NAME in errors[0]
    assert "secretRef.name" in errors[0]


def test_plain_secret_without_decryption_is_ok(tmp_path: Path) -> None:
    cluster = _cluster(tmp_path, _plain_secret(), FluxKustomizationSpec(path=_PATH))

    assert check_sops_decryption_blocks(cluster, tmp_path) == []


def test_suspended_kustomization_with_sops_is_skipped(tmp_path: Path) -> None:
    spec = FluxKustomizationSpec(path=_PATH, suspend=True)
    cluster = _cluster(tmp_path, _sops_secret(), spec)

    assert check_sops_decryption_blocks(cluster, tmp_path) == []


if __name__ == "__main__":
    pytest_bazel.main()
