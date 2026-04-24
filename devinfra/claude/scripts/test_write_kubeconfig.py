"""Tests for the standalone kubeconfig writer."""

import subprocess
from pathlib import Path

import pytest
import pytest_bazel
import yaml

from devinfra.claude.scripts import write_kubeconfig

_FAKE_TOKEN = "fake.jwt.token"

_KUBECONFIG = {
    "apiVersion": "v1",
    "kind": "Config",
    "clusters": [{"cluster": {"server": "https://k8s.example.com"}, "name": "cluster"}],
    "contexts": [{"context": {"cluster": "cluster", "namespace": "ns", "user": "u"}, "name": "u"}],
    "current-context": "u",
    "users": [{"name": "u", "user": {"token": _FAKE_TOKEN}}],
}


def test_build_kubeconfig_token() -> None:
    kc = write_kubeconfig.build_kubeconfig(
        token=_FAKE_TOKEN, server="https://k8s.example.com", user="test-user", namespace="secrets-ns"
    )
    assert kc["users"][0]["user"] == {"token": _FAKE_TOKEN}
    assert kc["clusters"][0]["cluster"] == {"server": "https://k8s.example.com"}
    assert kc["current-context"] == "test-user"


def test_write_kubeconfig_file_fresh(tmp_path: Path) -> None:
    output = tmp_path / "kubeconfig"
    write_kubeconfig.write_kubeconfig_file(_KUBECONFIG, output)
    assert yaml.safe_load(output.read_text()) == _KUBECONFIG
    assert output.stat().st_mode & 0o777 == 0o600


def test_write_kubeconfig_file_noop_when_identical(tmp_path: Path) -> None:
    output = tmp_path / "kubeconfig"
    write_kubeconfig.write_kubeconfig_file(_KUBECONFIG, output)
    mtime = output.stat().st_mtime_ns
    write_kubeconfig.write_kubeconfig_file(_KUBECONFIG, output)
    assert output.stat().st_mtime_ns == mtime


def test_write_kubeconfig_file_refuses_to_clobber(tmp_path: Path) -> None:
    output = tmp_path / "kubeconfig"
    other = {**_KUBECONFIG, "current-context": "different"}
    output.write_text(yaml.safe_dump(other))
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        write_kubeconfig.write_kubeconfig_file(_KUBECONFIG, output)
    assert yaml.safe_load(output.read_text()) == other


def test_write_kubeconfig_file_refuses_on_invalid_yaml(tmp_path: Path) -> None:
    output = tmp_path / "kubeconfig"
    output.write_text("not: valid: yaml: [")
    with pytest.raises(RuntimeError, match="not valid YAML"):
        write_kubeconfig.write_kubeconfig_file(_KUBECONFIG, output)


def _make_fake_sops(token: str = _FAKE_TOKEN):
    """Return a sops stub that returns the token value regardless of --extract arg."""

    def _fake_sops(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert cmd[0] == "sops"
        extract_arg = cmd[cmd.index("--extract") + 1]
        if "token" in extract_arg:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=token.encode(), stderr=b"")
        raise AssertionError(f"unexpected --extract arg: {extract_arg}")

    return _fake_sops


def test_main_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir = tmp_path / "repo"
    (project_dir / "secrets").mkdir(parents=True)
    (project_dir / "secrets" / "claude-web-k8s-token.yaml").write_text("stub")

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    monkeypatch.setattr(write_kubeconfig.subprocess, "run", _make_fake_sops())

    output_path = tmp_path / "out" / "kubeconfig"
    write_kubeconfig.main([str(output_path), "--server", "https://api.example.test:443"])

    kubeconfig = yaml.safe_load(output_path.read_text())
    assert kubeconfig["clusters"][0]["cluster"] == {"server": "https://api.example.test:443"}
    assert kubeconfig["users"][0]["user"] == {"token": _FAKE_TOKEN}
    assert kubeconfig["current-context"] == "claude-code-web"
    assert output_path.stat().st_mode & 0o777 == 0o600


def test_main_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Uses baked-in defaults when no --server/--user/--namespace given."""
    project_dir = tmp_path / "repo"
    (project_dir / "secrets").mkdir(parents=True)
    (project_dir / "secrets" / "claude-web-k8s-token.yaml").write_text("stub")

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    monkeypatch.setattr(write_kubeconfig.subprocess, "run", _make_fake_sops())

    output_path = tmp_path / "kubeconfig"
    write_kubeconfig.main([str(output_path)])

    kubeconfig = yaml.safe_load(output_path.read_text())
    assert kubeconfig["clusters"][0]["cluster"] == {"server": "https://kubeapi.allegedly.works"}
    assert kubeconfig["current-context"] == "claude-code-web"
    assert kubeconfig["users"][0]["user"] == {"token": _FAKE_TOKEN}


if __name__ == "__main__":
    pytest_bazel.main()
