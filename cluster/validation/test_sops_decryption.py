"""Tests for the SOPS-secret-without-decryption-block check."""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from cluster.validation.checks import check_sops_decryption_blocks
from cluster.validation.cluster import FluxBuild, ParsedCluster
from cluster.validation.flux import Decryption, FluxKustomizationSpec
from cluster.validation.k8s import SecretRef, parse_k8s_resources

_NAME = "synthetic"


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


def _cluster(secret_doc: dict, spec: FluxKustomizationSpec) -> ParsedCluster:
    return ParsedCluster(
        flux_kustomizations={_NAME: spec},
        build_results={_NAME: FluxBuild(Path("kustomization.yaml"), parse_k8s_resources([secret_doc]))},
    )


@pytest.mark.parametrize(
    ("decryption", "expected_error"),
    [
        (None, "decryption.provider: sops"),
        (Decryption(provider="sops"), "secretRef.name"),
        (Decryption(provider="sops", secret_ref=SecretRef(name="sops-age-cluster-secrets")), None),
    ],
    ids=["no-decryption", "provider-without-secretRef", "provider-with-secretRef"],
)
def test_sops_secret_requires_a_configured_decryption_key(
    decryption: Decryption | None, expected_error: str | None
) -> None:
    """Covers litellm-keys-tf (#2797): provider without secretRef leaves ciphertext unusable."""
    spec = FluxKustomizationSpec(decryption=decryption)
    errors = check_sops_decryption_blocks(_cluster(_sops_secret(), spec))
    if expected_error is None:
        assert errors == []
    else:
        assert len(errors) == 1
        assert _NAME in errors[0]
        assert expected_error in errors[0]


def test_plain_secret_without_decryption_is_ok() -> None:
    cluster = _cluster(_plain_secret(), FluxKustomizationSpec())

    assert check_sops_decryption_blocks(cluster) == []


def test_suspended_kustomization_with_sops_is_skipped() -> None:
    spec = FluxKustomizationSpec(suspend=True)
    cluster = _cluster(_sops_secret(), spec)

    assert check_sops_decryption_blocks(cluster) == []


if __name__ == "__main__":
    pytest_bazel.main()
