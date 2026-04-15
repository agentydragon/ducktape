"""Smoke test for `claude-hook write-kubeconfig` subcommand."""

import base64
import subprocess
from pathlib import Path
from textwrap import dedent

import pytest
import pytest_bazel
import yaml

from devinfra.claude.hook_daemon import write_kubeconfig_cli


def _write_profile(path: Path) -> None:
    path.write_text(
        dedent("""
            idle_watchdog: false
            k8s:
              server: https://api.example.test:443
              service_account: claude-code-web
              service_account_namespace: default
              namespace: claude-sandbox
        """).lstrip()
    )


def test_write_kubeconfig_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`claude-hook write-kubeconfig` writes a kubeconfig with CA + proxy-url set."""
    project_dir = tmp_path / "repo"
    (project_dir / "secrets").mkdir(parents=True)
    (project_dir / "secrets" / "claude-web-k8s-token.yaml").write_text("stub")

    profile_rel = "devinfra/claude/hook_daemon/profiles/test/profile.yaml"
    profile_path = project_dir / profile_rel
    profile_path.parent.mkdir(parents=True)
    _write_profile(profile_path)

    session_dir = tmp_path / "session"
    auth_proxy_dir = session_dir / "auth-proxy"
    auth_proxy_dir.mkdir(parents=True)
    ca_pem = "-----BEGIN CERTIFICATE-----\nFAKE-CA-DATA\n-----END CERTIFICATE-----\n"
    (auth_proxy_dir / "combined_ca.pem").write_text(ca_pem)

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    monkeypatch.setenv("DUCKTAPE_CLAUDE_HOOKS_PROFILE", profile_rel)
    monkeypatch.setenv("DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR", str(session_dir))
    monkeypatch.setenv("HTTPS_PROXY", "http://egress.example.test:3128")

    # Stub sops so the test doesn't need SOPS_AGE_KEY or a real encrypted file.
    def _fake_sops(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert cmd[0] == "sops"
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"fake-token\n", stderr=b"")

    monkeypatch.setattr(write_kubeconfig_cli.subprocess, "run", _fake_sops)

    output_path = tmp_path / "out" / "kubeconfig"
    write_kubeconfig_cli.main([str(output_path)])

    kubeconfig = yaml.safe_load(output_path.read_text())
    cluster = kubeconfig["clusters"][0]["cluster"]
    assert cluster["server"] == "https://api.example.test:443"
    assert cluster["proxy-url"] == "http://egress.example.test:3128"
    assert cluster["certificate-authority-data"] == base64.b64encode(ca_pem.encode()).decode()
    assert kubeconfig["users"][0]["user"]["token"] == "fake-token"
    assert output_path.stat().st_mode & 0o777 == 0o600


def test_write_kubeconfig_falls_back_to_system_ca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When no session combined_ca.pem, the system bundle is used (if present)."""
    project_dir = tmp_path / "repo"
    (project_dir / "secrets").mkdir(parents=True)
    (project_dir / "secrets" / "claude-web-k8s-token.yaml").write_text("stub")

    profile_rel = "profiles/test/profile.yaml"
    profile_path = project_dir / profile_rel
    profile_path.parent.mkdir(parents=True)
    _write_profile(profile_path)

    fake_system_ca = tmp_path / "system-ca.pem"
    fake_system_ca.write_text("-----BEGIN CERTIFICATE-----\nSYSTEM\n-----END CERTIFICATE-----\n")
    monkeypatch.setattr(write_kubeconfig_cli, "_SYSTEM_CA_BUNDLE", fake_system_ca)

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    monkeypatch.setenv("DUCKTAPE_CLAUDE_HOOKS_PROFILE", profile_rel)
    monkeypatch.delenv("DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)

    def _fake_sops(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=b"tok", stderr=b"")

    monkeypatch.setattr(write_kubeconfig_cli.subprocess, "run", _fake_sops)

    output_path = tmp_path / "kubeconfig"
    write_kubeconfig_cli.main([str(output_path)])

    kubeconfig = yaml.safe_load(output_path.read_text())
    cluster = kubeconfig["clusters"][0]["cluster"]
    assert "certificate-authority-data" in cluster
    assert "proxy-url" not in cluster


if __name__ == "__main__":
    pytest_bazel.main()
