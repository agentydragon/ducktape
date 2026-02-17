"""Unit tests for secrets_setup module."""

from __future__ import annotations

import json
from pathlib import Path

import pyrage
import pytest
import pytest_bazel

from tools.claude_hooks.kubeconfig_setup import KubeconfigSecret
from tools.claude_hooks.secrets_setup import SecretsSetup, setup_secrets


def _encrypt_json(data: dict[str, str], recipient: pyrage.x25519.Recipient) -> bytes:
    """Encrypt a JSON dict to an age recipient."""
    result: bytes = pyrage.encrypt(json.dumps(data).encode(), [recipient])
    return result


@pytest.fixture
def age_keypair() -> tuple[pyrage.x25519.Identity, pyrage.x25519.Recipient]:
    """Generate a fresh age keypair for testing."""
    identity = pyrage.x25519.Identity.generate()
    recipient = identity.to_public()
    return identity, recipient


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    """Create a secrets directory."""
    d = tmp_path / "secrets"
    d.mkdir()
    return d


def test_no_age_key_returns_none(secrets_dir: Path) -> None:
    result = setup_secrets(age_key=None, secrets_dir=secrets_dir)
    assert result is None


def test_missing_dir_returns_none() -> None:
    result = setup_secrets(age_key="AGE-SECRET-KEY-1FAKE", secrets_dir=Path("/nonexistent"))
    assert result is None


def test_successful_decryption(
    secrets_dir: Path, age_keypair: tuple[pyrage.x25519.Identity, pyrage.x25519.Recipient]
) -> None:
    identity, recipient = age_keypair
    (secrets_dir / "test.age").write_bytes(_encrypt_json({"FOO": "bar"}, recipient))

    result = setup_secrets(age_key=str(identity), secrets_dir=secrets_dir)

    assert result is not None
    assert result.env_vars == {"FOO": "bar"}
    assert result.skipped_files == []


def test_all_files_skipped_on_wrong_key(
    secrets_dir: Path, age_keypair: tuple[pyrage.x25519.Identity, pyrage.x25519.Recipient]
) -> None:
    """When age_key doesn't match any file, all files are skipped."""
    _, recipient = age_keypair
    (secrets_dir / "a.age").write_bytes(_encrypt_json({"A": "1"}, recipient))
    (secrets_dir / "b.age").write_bytes(_encrypt_json({"B": "2"}, recipient))

    # Use a different key that won't match
    wrong_identity = pyrage.x25519.Identity.generate()

    result = setup_secrets(age_key=str(wrong_identity), secrets_dir=secrets_dir)

    assert result is not None
    assert result.env_vars == {}
    assert sorted(result.skipped_files) == ["a.age", "b.age"]


def test_partial_decryption_tracks_skipped(secrets_dir: Path) -> None:
    """When some files match and others don't, skipped files are tracked."""
    id1 = pyrage.x25519.Identity.generate()
    id2 = pyrage.x25519.Identity.generate()

    # Encrypt one file to id1, another to id2
    (secrets_dir / "matches.age").write_bytes(_encrypt_json({"MATCH": "yes"}, id1.to_public()))
    (secrets_dir / "nomatch.age").write_bytes(_encrypt_json({"SKIP": "no"}, id2.to_public()))

    result = setup_secrets(age_key=str(id1), secrets_dir=secrets_dir)

    assert result is not None
    assert result.env_vars == {"MATCH": "yes"}
    assert result.skipped_files == ["nomatch.age"]


def test_duplicate_keys_across_files_raises(
    secrets_dir: Path, age_keypair: tuple[pyrage.x25519.Identity, pyrage.x25519.Recipient]
) -> None:
    identity, recipient = age_keypair
    (secrets_dir / "a.age").write_bytes(_encrypt_json({"DUP": "1"}, recipient))
    (secrets_dir / "b.age").write_bytes(_encrypt_json({"DUP": "2"}, recipient))

    with pytest.raises(ValueError, match="Duplicate env var keys"):
        setup_secrets(age_key=str(identity), secrets_dir=secrets_dir)


def test_empty_dir_returns_empty(
    secrets_dir: Path, age_keypair: tuple[pyrage.x25519.Identity, pyrage.x25519.Recipient]
) -> None:
    identity, _ = age_keypair
    result = setup_secrets(age_key=str(identity), secrets_dir=secrets_dir)

    assert result is not None
    assert result.env_vars == {}
    assert result.skipped_files == []


def test_secrets_setup_defaults() -> None:
    """Verify SecretsSetup defaults."""
    setup = SecretsSetup()
    assert setup.env_vars == {}
    assert setup.kubeconfig is None
    assert setup.skipped_files == []


def test_kubeconfig_typed_secret(
    secrets_dir: Path, age_keypair: tuple[pyrage.x25519.Identity, pyrage.x25519.Recipient]
) -> None:
    """Typed kubeconfig secret is parsed into secrets.kubeconfig, not env_vars."""
    identity, recipient = age_keypair
    payload = {
        "type": "kubeconfig",
        "server": "https://allegedly.works:6443",
        "ca_b64": "dGVzdC1jYQ==",
        "token": "my-token",
    }
    (secrets_dir / "kubeconfig.age").write_bytes(_encrypt_json(payload, recipient))

    result = setup_secrets(age_key=str(identity), secrets_dir=secrets_dir)

    assert result is not None
    assert result.kubeconfig == KubeconfigSecret(
        server="https://allegedly.works:6443", ca_b64="dGVzdC1jYQ==", token="my-token"
    )
    # Not exported to shell
    assert "KUBE" not in result.env_vars


def test_kubeconfig_secret_not_in_env_exports(
    secrets_dir: Path, age_keypair: tuple[pyrage.x25519.Identity, pyrage.x25519.Recipient]
) -> None:
    """Kubeconfig secret combined with flat secrets: only flat vars exported."""
    identity, recipient = age_keypair
    (secrets_dir / "a.age").write_bytes(
        _encrypt_json({"type": "kubeconfig", "server": "https://k8s", "ca_b64": "Y2E=", "token": "tok"}, recipient)
    )
    (secrets_dir / "b.age").write_bytes(_encrypt_json({"API_KEY": "secret"}, recipient))

    result = setup_secrets(age_key=str(identity), secrets_dir=secrets_dir)

    assert result is not None
    assert result.kubeconfig is not None
    assert result.env_vars == {"API_KEY": "secret"}


def test_duplicate_kubeconfig_raises(
    secrets_dir: Path, age_keypair: tuple[pyrage.x25519.Identity, pyrage.x25519.Recipient]
) -> None:
    """Two kubeconfig secrets in the same secrets dir raises an error."""
    identity, recipient = age_keypair
    kube_payload = {"type": "kubeconfig", "server": "https://k8s", "ca_b64": "Y2E=", "token": "tok"}
    (secrets_dir / "a.age").write_bytes(_encrypt_json(kube_payload, recipient))
    (secrets_dir / "b.age").write_bytes(_encrypt_json(kube_payload, recipient))

    with pytest.raises(ValueError, match="Duplicate kubeconfig secret"):
        setup_secrets(age_key=str(identity), secrets_dir=secrets_dir)


def test_unknown_type_raises(
    secrets_dir: Path, age_keypair: tuple[pyrage.x25519.Identity, pyrage.x25519.Recipient]
) -> None:
    """An unknown 'type' value raises ValueError."""
    identity, recipient = age_keypair
    (secrets_dir / "weird.age").write_bytes(_encrypt_json({"type": "spaceship", "fuel": "dilithium"}, recipient))

    with pytest.raises(ValueError, match="Unknown secret type"):
        setup_secrets(age_key=str(identity), secrets_dir=secrets_dir)


if __name__ == "__main__":
    pytest_bazel.main()
