"""Tests for k8s_secrets_setup."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytest_bazel

from devinfra.claude_hooks.k8s_secrets_setup import (
    HookConfig,
    K8sConfig,
    K8sSecretMapping,
    K8sSecretsConfig,
    setup_k8s_secrets,
)


def _make_config(secrets: list[K8sSecretMapping]) -> HookConfig:
    return HookConfig(
        k8s=K8sConfig(
            server="https://k8s.example.com", service_account="test-sa", sa_namespace="default", namespace="secrets-ns"
        ),
        k8s_secrets=K8sSecretsConfig(namespace="secrets-ns", secrets=secrets),
    )


def _make_mock_k8s_secret(data: dict[str, str]) -> MagicMock:
    """Create a mock k8s Secret with base64-encoded data."""
    secret = MagicMock()
    secret.data = {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}
    return secret


@pytest.fixture
def mock_k8s_api() -> MagicMock:
    """Mock the kubernetes CoreV1Api."""
    with (
        patch("devinfra.claude_hooks.k8s_secrets_setup.k8s_client"),
        patch("devinfra.claude_hooks.k8s_secrets_setup.CoreV1Api") as mock_api_cls,
    ):
        mock_api = MagicMock()
        mock_api_cls.return_value = mock_api
        yield mock_api


def test_duplicate_env_var_across_secrets_raises(tmp_path: Path, mock_k8s_api: MagicMock) -> None:
    """Two secrets mapping different keys to the same env var name should raise ValueError."""
    config = _make_config(
        [
            K8sSecretMapping(name="secret-a", data={"key1": "MY_VAR"}),
            K8sSecretMapping(name="secret-b", data={"key2": "MY_VAR"}),
        ]
    )
    mock_k8s_api.read_namespaced_secret.side_effect = [
        _make_mock_k8s_secret({"key1": "value1"}),
        _make_mock_k8s_secret({"key2": "value2"}),
    ]

    with pytest.raises(ValueError, match="Duplicate env var 'MY_VAR'") as exc_info:
        setup_k8s_secrets(token="tok", session_dir=tmp_path, combined_ca_path=None, config=config)
    assert "secret-a" in str(exc_info.value)
    assert "secret-b" in str(exc_info.value)


def test_duplicate_env_var_within_same_secret_raises(tmp_path: Path, mock_k8s_api: MagicMock) -> None:
    """Two different keys within the same secret mapping to the same env var name should raise ValueError."""
    config = _make_config([K8sSecretMapping(name="secret-a", data={"key1": "MY_VAR", "key2": "MY_VAR"})])
    mock_k8s_api.read_namespaced_secret.return_value = _make_mock_k8s_secret({"key1": "value1", "key2": "value2"})

    with pytest.raises(ValueError, match="Duplicate env var 'MY_VAR'"):
        setup_k8s_secrets(token="tok", session_dir=tmp_path, combined_ca_path=None, config=config)


def test_unique_env_vars_succeeds(tmp_path: Path, mock_k8s_api: MagicMock) -> None:
    """Multiple secrets with distinct env var names should succeed without error."""
    config = _make_config(
        [
            K8sSecretMapping(name="secret-a", data={"key1": "VAR_A"}),
            K8sSecretMapping(name="secret-b", data={"key2": "VAR_B"}),
        ]
    )
    mock_k8s_api.read_namespaced_secret.side_effect = [
        _make_mock_k8s_secret({"key1": "value_a"}),
        _make_mock_k8s_secret({"key2": "value_b"}),
    ]

    result = setup_k8s_secrets(token="tok", session_dir=tmp_path, combined_ca_path=None, config=config)

    assert result.env_vars["VAR_A"] == "value_a"
    assert result.env_vars["VAR_B"] == "value_b"


if __name__ == "__main__":
    pytest_bazel.main()
