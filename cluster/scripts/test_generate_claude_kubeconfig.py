"""Tests for kubeconfig generation with K8s token authentication."""

from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytest_bazel
import yaml
from kubernetes import client

from cluster.scripts.generate_claude_kubeconfig import (
    SA_NAMESPACE,
    SERVER,
    SERVICE_ACCOUNT,
    TOKEN_EXPIRY_SECONDS,
    generate,
)


class TestGenerateKubeconfig:
    """Tests for kubeconfig generation with K8s token authentication."""

    def test_generate_requires_recipients_file(self, tmp_path: Path, mocker) -> None:
        """Verify that generate() fails if recipients.txt is missing."""
        root = tmp_path
        root.joinpath(".claude_hooks", "secrets").mkdir(parents=True)
        mocker.patch.dict("os.environ", {"KUBECONFIG": "/tmp/fake-kubeconfig"})

        with pytest.raises(SystemExit, match=r"No recipients\.txt found"):
            generate(root)

    def test_generate_requires_kubeconfig_env(self, tmp_path: Path, mocker) -> None:
        """Verify that generate() fails if KUBECONFIG env is not set."""
        root = tmp_path
        root.joinpath(".claude_hooks", "secrets").mkdir(parents=True)
        recipients_file = root / ".claude_hooks" / "secrets" / "recipients.txt"
        recipients_file.write_text("age1test123")

        # Ensure KUBECONFIG is not set
        mocker.patch.dict("os.environ", {}, clear=True)

        with pytest.raises(SystemExit, match=r"KUBECONFIG not set"):
            generate(root)

    def test_generate_creates_encrypted_kubeconfig(self, tmp_path: Path, mocker) -> None:
        """Verify that generate() creates an age-encrypted kubeconfig.age file."""
        root = tmp_path
        root.joinpath(".claude_hooks", "secrets").mkdir(parents=True)

        # Create a recipients file
        recipients_file = root / ".claude_hooks" / "secrets" / "recipients.txt"
        recipients_file.write_text("age1test123456789")

        # Create a fake admin kubeconfig
        fake_kubeconfig_path = tmp_path / "kubeconfig"
        ca_cert = "-----BEGIN CERTIFICATE-----\nMIIDCTCCAfGgAwIBAgIUApL5xM+4\n-----END CERTIFICATE-----\n"
        ca_b64 = base64.b64encode(ca_cert.encode()).decode()
        admin_kubeconfig = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [{"cluster": {"certificate-authority-data": ca_b64, "server": SERVER}, "name": "admin"}],
            "current-context": "admin",
            "contexts": [{"context": {"cluster": "admin", "user": "admin"}, "name": "admin"}],
            "users": [{"name": "admin", "user": {"token": "admin-token"}}],
        }
        fake_kubeconfig_path.write_text(yaml.dump(admin_kubeconfig))

        # Mock Kubernetes API
        mock_token_response = MagicMock()
        mock_token_response.status.token = "generated-token-value"

        # Mock pyrage functions
        mock_recipient = MagicMock()
        mock_encrypted_data = b"encrypted-data"

        with (
            patch("cluster.scripts.generate_claude_kubeconfig.config"),
            patch("cluster.scripts.generate_claude_kubeconfig.client.CoreV1Api") as mock_api_class,
            patch(
                "cluster.scripts.generate_claude_kubeconfig.pyrage.x25519.Recipient.from_str"
            ) as mock_recipient_from_str,
            patch("cluster.scripts.generate_claude_kubeconfig.pyrage.encrypt") as mock_encrypt,
        ):
            mock_api = MagicMock()
            mock_api.create_namespaced_service_account_token.return_value = mock_token_response
            mock_api_class.return_value = mock_api

            mock_recipient_from_str.return_value = mock_recipient
            mock_encrypt.return_value = mock_encrypted_data

            mocker.patch.dict("os.environ", {"KUBECONFIG": str(fake_kubeconfig_path)})

            # Call generate
            generate(root)

            # Verify the API was called correctly
            mock_api.create_namespaced_service_account_token.assert_called_once()
            call_args = mock_api.create_namespaced_service_account_token.call_args
            assert call_args[0][0] == SERVICE_ACCOUNT
            assert call_args[0][1] == SA_NAMESPACE
            token_request = call_args[0][2]
            assert isinstance(token_request, client.AuthenticationV1TokenRequest)
            assert token_request.spec.audiences == ["https://kubernetes.default.svc"]
            assert token_request.spec.expiration_seconds == TOKEN_EXPIRY_SECONDS

        # Verify encrypted file was created
        output_file = root / ".claude_hooks" / "secrets" / "kubeconfig.age"
        assert output_file.exists()
        assert output_file.read_bytes() == mock_encrypted_data

    def test_token_request_includes_audience(self, tmp_path: Path, mocker) -> None:
        """Verify token request includes the correct audience for API server binding."""
        root = tmp_path
        root.joinpath(".claude_hooks", "secrets").mkdir(parents=True)

        # Create a recipients file
        recipients_file = root / ".claude_hooks" / "secrets" / "recipients.txt"
        recipients_file.write_text("age1test123456789")

        # Create a fake admin kubeconfig
        fake_kubeconfig_path = tmp_path / "kubeconfig"
        ca_b64 = base64.b64encode(b"cert").decode()
        admin_kubeconfig = {
            "apiVersion": "v1",
            "kind": "Config",
            "clusters": [{"cluster": {"certificate-authority-data": ca_b64, "server": SERVER}, "name": "admin"}],
            "current-context": "admin",
            "contexts": [{"context": {"cluster": "admin", "user": "admin"}, "name": "admin"}],
            "users": [{"name": "admin", "user": {"token": "admin-token"}}],
        }
        fake_kubeconfig_path.write_text(yaml.dump(admin_kubeconfig))

        # Mock Kubernetes API
        mock_token_response = MagicMock()
        mock_token_response.status.token = "test-token"

        with (
            patch("cluster.scripts.generate_claude_kubeconfig.config"),
            patch("cluster.scripts.generate_claude_kubeconfig.client.CoreV1Api") as mock_api_class,
            patch("cluster.scripts.generate_claude_kubeconfig.pyrage.x25519.Recipient.from_str"),
            patch("cluster.scripts.generate_claude_kubeconfig.pyrage.encrypt") as mock_encrypt,
        ):
            mock_encrypt.return_value = b"encrypted-data"
            mock_api = MagicMock()
            mock_api.create_namespaced_service_account_token.return_value = mock_token_response
            mock_api_class.return_value = mock_api

            mocker.patch.dict("os.environ", {"KUBECONFIG": str(fake_kubeconfig_path)})
            generate(root)

            # Verify the audience is bound to the Kubernetes API server
            call_args = mock_api.create_namespaced_service_account_token.call_args
            token_request = call_args[0][2]
            assert "https://kubernetes.default.svc" in token_request.spec.audiences


if __name__ == "__main__":
    pytest_bazel.main()
