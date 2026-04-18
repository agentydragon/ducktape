"""Tests for the standalone kubeconfig writer."""

import base64
import subprocess
from pathlib import Path

import pytest
import pytest_bazel
import yaml

from devinfra.claude.scripts import write_kubeconfig


def test_build_kubeconfig_proxy_url() -> None:
    kc = write_kubeconfig.build_kubeconfig(
        token="tok",
        server="https://k8s.example.com",
        service_account="test-sa",
        namespace="secrets-ns",
        ca_path=None,
        proxy_url="http://localhost:18081",
    )
    assert kc["clusters"][0]["cluster"]["proxy-url"] == "http://localhost:18081"


def test_build_kubeconfig_no_proxy_url() -> None:
    kc = write_kubeconfig.build_kubeconfig(
        token="tok",
        server="https://k8s.example.com",
        service_account="test-sa",
        namespace="secrets-ns",
        ca_path=None,
        proxy_url=None,
    )
    assert "proxy-url" not in kc["clusters"][0]["cluster"]


def test_build_kubeconfig_ca_data(tmp_path: Path) -> None:
    ca_file = tmp_path / "ca.pem"
    ca_file.write_text("-----BEGIN CERTIFICATE-----\nfake\n-----END CERTIFICATE-----\n")
    kc = write_kubeconfig.build_kubeconfig(
        token="tok",
        server="https://k8s.example.com",
        service_account="test-sa",
        namespace="secrets-ns",
        ca_path=ca_file,
        proxy_url=None,
    )
    assert "certificate-authority-data" in kc["clusters"][0]["cluster"]


def test_main_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir = tmp_path / "repo"
    (project_dir / "secrets").mkdir(parents=True)
    (project_dir / "secrets" / "claude-web-k8s-token.yaml").write_text("stub")

    fake_system_ca = tmp_path / "system-ca.pem"
    ca_pem = "-----BEGIN CERTIFICATE-----\nFAKE-CA-DATA\n-----END CERTIFICATE-----\n"
    fake_system_ca.write_text(ca_pem)
    monkeypatch.setattr(write_kubeconfig, "_SYSTEM_CA_BUNDLE", fake_system_ca)

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    monkeypatch.setenv("HTTPS_PROXY", "http://egress.example.test:3128")

    def _fake_sops(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert cmd[0] == "sops"
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"fake-token\n", stderr=b"")

    monkeypatch.setattr(write_kubeconfig.subprocess, "run", _fake_sops)

    output_path = tmp_path / "out" / "kubeconfig"
    write_kubeconfig.main([str(output_path), "--server", "https://api.example.test:443"])

    kubeconfig = yaml.safe_load(output_path.read_text())
    cluster = kubeconfig["clusters"][0]["cluster"]
    assert cluster["server"] == "https://api.example.test:443"
    assert cluster["proxy-url"] == "http://egress.example.test:3128"
    assert cluster["certificate-authority-data"] == base64.b64encode(ca_pem.encode()).decode()
    assert kubeconfig["users"][0]["user"]["token"] == "fake-token"
    assert kubeconfig["current-context"] == "claude-code-web"
    assert output_path.stat().st_mode & 0o777 == 0o600


def test_main_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Uses baked-in defaults when no --server/--service-account/--namespace given."""
    project_dir = tmp_path / "repo"
    (project_dir / "secrets").mkdir(parents=True)
    (project_dir / "secrets" / "claude-web-k8s-token.yaml").write_text("stub")

    monkeypatch.setattr(write_kubeconfig, "_SYSTEM_CA_BUNDLE", tmp_path / "nonexistent")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)

    def _fake_sops(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"tok", stderr=b"")

    monkeypatch.setattr(write_kubeconfig.subprocess, "run", _fake_sops)

    output_path = tmp_path / "kubeconfig"
    write_kubeconfig.main([str(output_path)])

    kubeconfig = yaml.safe_load(output_path.read_text())
    cluster = kubeconfig["clusters"][0]["cluster"]
    assert cluster["server"] == "https://api.allegedly.works"
    assert kubeconfig["current-context"] == "claude-code-web"
    assert "proxy-url" not in cluster


if __name__ == "__main__":
    pytest_bazel.main()
