"""Tests for k8s_secrets_setup."""

import base64
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytest_bazel
import yaml

from devinfra.claude.hook_config import HookConfig, K8sConfig, K8sSecretMapping, K8sSecretsConfig
from devinfra.claude.hook_daemon.session_start.k8s_secrets import setup_k8s_secrets


def _make_config(secrets: list[K8sSecretMapping]) -> HookConfig:
    return HookConfig(
        k8s=K8sConfig(
            server="https://k8s.example.com",
            service_account="test-sa",
            service_account_namespace="default",
            namespace="secrets-ns",
        ),
        k8s_secrets=K8sSecretsConfig(namespace="secrets-ns", secrets=secrets),
    )


def _make_mock_k8s_secret(data: dict[str, str]) -> MagicMock:
    """Create a mock k8s Secret with base64-encoded data."""
    secret = MagicMock()
    secret.data = {k: base64.b64encode(v.encode()).decode() for k, v in data.items()}
    return secret


@pytest.fixture
def mock_k8s_api() -> Generator[MagicMock]:
    """Mock the kubernetes CoreV1Api."""
    with (
        patch("devinfra.claude.hook_daemon.session_start.k8s_secrets.k8s_client"),
        patch("devinfra.claude.hook_daemon.session_start.k8s_secrets.CoreV1Api") as mock_api_cls,
    ):
        mock_api = MagicMock()
        mock_api_cls.return_value = mock_api
        yield mock_api


def test_duplicate_env_var_across_secrets_raises() -> None:
    """Two secrets mapping different keys to the same env var name should raise at config construction."""
    with pytest.raises(ValueError, match="Duplicate env var 'MY_VAR'"):
        _make_config(
            [
                K8sSecretMapping(name="secret-a", data={"key1": "MY_VAR"}),
                K8sSecretMapping(name="secret-b", data={"key2": "MY_VAR"}),
            ]
        )


def test_duplicate_env_var_within_same_secret_raises() -> None:
    """Two different keys within the same secret mapping to the same env var name should raise."""
    with pytest.raises(ValueError, match="Duplicate env var 'MY_VAR'"):
        _make_config([K8sSecretMapping(name="secret-a", data={"key1": "MY_VAR", "key2": "MY_VAR"})])


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


def test_kubeconfig_proxy_url(tmp_path: Path, mock_k8s_api: MagicMock) -> None:
    """When proxy is set, kubeconfig should include proxy-url in the cluster config."""
    config = _make_config([])
    mock_k8s_api.read_namespaced_secret.side_effect = []

    result = setup_k8s_secrets(
        token="tok", session_dir=tmp_path, combined_ca_path=None, config=config, proxy="http://localhost:18081"
    )

    assert result.kubeconfig_path is not None
    kubeconfig = yaml.safe_load(result.kubeconfig_path.read_text())
    assert kubeconfig["clusters"][0]["cluster"]["proxy-url"] == "http://localhost:18081"


def test_kubeconfig_no_proxy_url_when_unset(
    tmp_path: Path, mock_k8s_api: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When proxy is not set and no proxy env vars are present, kubeconfig should not include proxy-url."""
    config = _make_config([])
    mock_k8s_api.read_namespaced_secret.side_effect = []
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)

    result = setup_k8s_secrets(token="tok", session_dir=tmp_path, combined_ca_path=None, config=config)

    assert result.kubeconfig_path is not None
    kubeconfig = yaml.safe_load(result.kubeconfig_path.read_text())
    assert "proxy-url" not in kubeconfig["clusters"][0]["cluster"]


def test_kubeconfig_proxy_url_from_env(
    tmp_path: Path, mock_k8s_api: MagicMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When no explicit proxy is given but HTTPS_PROXY is set, kubeconfig should use it."""
    config = _make_config([])
    mock_k8s_api.read_namespaced_secret.side_effect = []
    monkeypatch.setenv("HTTPS_PROXY", "http://egress-proxy:15004")
    monkeypatch.delenv("HTTP_PROXY", raising=False)

    result = setup_k8s_secrets(token="tok", session_dir=tmp_path, combined_ca_path=None, config=config)

    assert result.kubeconfig_path is not None
    kubeconfig = yaml.safe_load(result.kubeconfig_path.read_text())
    assert kubeconfig["clusters"][0]["cluster"]["proxy-url"] == "http://egress-proxy:15004"


if __name__ == "__main__":
    pytest_bazel.main()
