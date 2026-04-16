"""Tests for kubeconfig generation."""

from pathlib import Path

import pytest_bazel

from devinfra.claude.hook_daemon.write_kubeconfig_cli import build_kubeconfig


def test_kubeconfig_proxy_url() -> None:
    """When proxy is set, kubeconfig should include proxy-url in the cluster config."""
    kc = build_kubeconfig(
        token="tok",
        server="https://k8s.example.com",
        service_account="test-sa",
        namespace="secrets-ns",
        ca_path=None,
        proxy_url="http://localhost:18081",
    )
    assert kc["clusters"][0]["cluster"]["proxy-url"] == "http://localhost:18081"


def test_kubeconfig_no_proxy_url_when_unset() -> None:
    """When proxy is not set, kubeconfig should not include proxy-url."""
    kc = build_kubeconfig(
        token="tok",
        server="https://k8s.example.com",
        service_account="test-sa",
        namespace="secrets-ns",
        ca_path=None,
        proxy_url=None,
    )
    assert "proxy-url" not in kc["clusters"][0]["cluster"]


def test_kubeconfig_ca_data(tmp_path: Path) -> None:
    """When CA path exists, kubeconfig should include certificate-authority-data."""
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
    kc = build_kubeconfig(
        token="tok",
        server="https://k8s.example.com",
        service_account="test-sa",
        namespace="secrets-ns",
        ca_path=ca_file,
        proxy_url=None,
    )
    assert "certificate-authority-data" in kc["clusters"][0]["cluster"]


if __name__ == "__main__":
    pytest_bazel.main()
