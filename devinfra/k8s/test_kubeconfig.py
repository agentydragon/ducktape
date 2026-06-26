"""Tests for the standalone kubeconfig writer."""

import subprocess
from pathlib import Path

import pytest
import pytest_bazel
import yaml

from devinfra.k8s import kubeconfig

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
    kc = kubeconfig.build_kubeconfig(_FAKE_TOKEN)
    assert kc["users"][0]["user"] == {"token": _FAKE_TOKEN}
    assert kc["clusters"][0]["cluster"] == {"server": kubeconfig.DEFAULT_SERVER}
    assert kc["current-context"] == kubeconfig.DEFAULT_USER
    assert kc["contexts"][0]["context"]["namespace"] == kubeconfig.DEFAULT_NAMESPACE


def test_build_kubeconfig_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("K8S_USER", "haku-k8s")
    monkeypatch.setenv("K8S_NAMESPACE", "haku")
    kc = kubeconfig.build_kubeconfig(_FAKE_TOKEN)
    assert kc["current-context"] == "haku-k8s"
    assert kc["users"][0]["name"] == "haku-k8s"
    assert kc["contexts"][0]["context"] == {"cluster": "cluster", "namespace": "haku", "user": "haku-k8s"}


def test_write_kubeconfig_file_fresh(tmp_path: Path) -> None:
    output = tmp_path / "kubeconfig"
    kubeconfig.write_kubeconfig_file(_KUBECONFIG, output)
    assert yaml.safe_load(output.read_text()) == _KUBECONFIG
    assert output.stat().st_mode & 0o777 == 0o600


def test_write_kubeconfig_file_noop_when_identical(tmp_path: Path) -> None:
    output = tmp_path / "kubeconfig"
    kubeconfig.write_kubeconfig_file(_KUBECONFIG, output)
    mtime = output.stat().st_mtime_ns
    kubeconfig.write_kubeconfig_file(_KUBECONFIG, output)
    assert output.stat().st_mtime_ns == mtime


def test_write_kubeconfig_file_overwrites_empty(tmp_path: Path) -> None:
    """An empty existing file (e.g., from `mktemp`) is treated as a fresh write."""
    output = tmp_path / "kubeconfig"
    output.write_text("")
    kubeconfig.write_kubeconfig_file(_KUBECONFIG, output)
    assert yaml.safe_load(output.read_text()) == _KUBECONFIG
    assert output.stat().st_mode & 0o777 == 0o600


def _kubeconfig_with(**overrides: object) -> dict:
    """Return a copy of _KUBECONFIG with top-level keys replaced."""
    return {**_KUBECONFIG, **overrides}


def _kubeconfig_with_user(user: str) -> dict:
    kc = {**_KUBECONFIG}
    kc["users"] = [{"name": user, "user": {"token": _FAKE_TOKEN}}]
    kc["contexts"] = [{"context": {"cluster": "cluster", "namespace": "ns", "user": user}, "name": user}]
    kc["current-context"] = user
    return kc


def test_write_kubeconfig_file_refuses_different_user(tmp_path: Path) -> None:
    """Different user identity → refuse without probing the server."""
    output = tmp_path / "kubeconfig"
    output.write_text(yaml.safe_dump(_kubeconfig_with_user("someone-else")))
    with pytest.raises(RuntimeError, match="user.*someone-else"):
        kubeconfig.write_kubeconfig_file(_KUBECONFIG, output)
    assert yaml.safe_load(output.read_text()) == _kubeconfig_with_user("someone-else")


def test_write_kubeconfig_file_refuses_different_server(tmp_path: Path) -> None:
    """Different server → refuse without probing."""
    output = tmp_path / "kubeconfig"
    other = {**_KUBECONFIG, "clusters": [{"cluster": {"server": "https://other.example.com"}, "name": "cluster"}]}
    output.write_text(yaml.safe_dump(other))
    with pytest.raises(RuntimeError, match="server"):
        kubeconfig.write_kubeconfig_file(_KUBECONFIG, output)


def test_write_kubeconfig_file_refuses_merged_config(tmp_path: Path) -> None:
    """Merged kubeconfig (multiple users) → refuse without probing."""
    output = tmp_path / "kubeconfig"
    merged = {
        **_KUBECONFIG,
        "users": [
            {"name": "u", "user": {"token": _FAKE_TOKEN}},
            {"name": "admin", "user": {"token": "admin-token"}},
        ],
    }
    output.write_text(yaml.safe_dump(merged))
    with pytest.raises(RuntimeError, match="2 users"):
        kubeconfig.write_kubeconfig_file(_KUBECONFIG, output)


def test_write_kubeconfig_file_token_refresh_allowed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same server+user, different token, new token valid → overwrite allowed."""
    output = tmp_path / "kubeconfig"
    output.write_text(yaml.safe_dump(_KUBECONFIG))
    monkeypatch.setattr(kubeconfig, "_probe_token", lambda server, token, **_: "valid")
    new_kc = {**_KUBECONFIG, "users": [{"name": "u", "user": {"token": "rotated.jwt.token"}}]}
    kubeconfig.write_kubeconfig_file(new_kc, output)
    assert yaml.safe_load(output.read_text()) == new_kc


def test_write_kubeconfig_file_refuses_new_token_invalid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Same server+user, different token, new token returns 401 → refuse."""
    output = tmp_path / "kubeconfig"
    output.write_text(yaml.safe_dump(_KUBECONFIG))
    monkeypatch.setattr(kubeconfig, "_probe_token", lambda server, token, **_: "invalid")
    new_kc = {**_KUBECONFIG, "users": [{"name": "u", "user": {"token": "bad.token"}}]}
    with pytest.raises(RuntimeError, match="new token is rejected"):
        kubeconfig.write_kubeconfig_file(new_kc, output)
    assert yaml.safe_load(output.read_text()) == _KUBECONFIG


def test_write_kubeconfig_file_token_refresh_server_unreachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server unreachable (probe returns None) → allow overwrite (can't verify, proceed)."""
    output = tmp_path / "kubeconfig"
    output.write_text(yaml.safe_dump(_KUBECONFIG))
    monkeypatch.setattr(kubeconfig, "_probe_token", lambda server, token, **_: None)
    new_kc = {**_KUBECONFIG, "users": [{"name": "u", "user": {"token": "rotated.jwt.token"}}]}
    kubeconfig.write_kubeconfig_file(new_kc, output)
    assert yaml.safe_load(output.read_text()) == new_kc


def test_write_kubeconfig_file_refuses_on_invalid_yaml(tmp_path: Path) -> None:
    output = tmp_path / "kubeconfig"
    output.write_text("not: valid: yaml: [")
    with pytest.raises(RuntimeError, match="not valid YAML"):
        kubeconfig.write_kubeconfig_file(_KUBECONFIG, output)


def _make_fake_sops(token: str = _FAKE_TOKEN):
    """Return a sops stub that returns the token value regardless of --extract arg."""

    def _fake_sops(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        assert cmd[0] == "sops"
        extract_arg = cmd[cmd.index("--extract") + 1]
        if "jwt" in extract_arg:
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=token.encode(), stderr=b"")
        raise AssertionError(f"unexpected --extract arg: {extract_arg}")

    return _fake_sops


def test_main_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project_dir = tmp_path / "repo"
    (project_dir / "secrets").mkdir(parents=True)
    (project_dir / "secrets" / "claude-web-k8s-jwt.yaml").write_text("stub")

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    monkeypatch.setattr(kubeconfig.subprocess, "run", _make_fake_sops())

    output_path = tmp_path / "out" / "kubeconfig"
    kubeconfig.main(["--write", str(output_path)])

    kc = yaml.safe_load(output_path.read_text())
    assert kc["clusters"][0]["cluster"] == {"server": "https://kubeapi.allegedly.works"}
    assert kc["users"][0]["user"] == {"token": _FAKE_TOKEN}
    assert kc["current-context"] == "claude-code-web"
    assert output_path.stat().st_mode & 0o777 == 0o600


def test_main_end_to_end_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """K8S_JWT_SOPS_PATH / K8S_USER / K8S_NAMESPACE retarget the writer (e.g. haku)."""
    project_dir = tmp_path / "repo"
    (project_dir / "secrets").mkdir(parents=True)
    (project_dir / "secrets" / "haku-k8s-jwt.yaml").write_text("stub")

    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    monkeypatch.setenv("K8S_JWT_SOPS_PATH", "secrets/haku-k8s-jwt.yaml")
    monkeypatch.setenv("K8S_USER", "haku-k8s")
    monkeypatch.setenv("K8S_NAMESPACE", "haku")
    monkeypatch.setattr(kubeconfig.subprocess, "run", _make_fake_sops())

    output_path = tmp_path / "out" / "kubeconfig"
    kubeconfig.main(["--write", str(output_path)])

    kc = yaml.safe_load(output_path.read_text())
    assert kc["current-context"] == "haku-k8s"
    assert kc["contexts"][0]["context"]["namespace"] == "haku"
    assert kc["users"][0]["user"] == {"token": _FAKE_TOKEN}


if __name__ == "__main__":
    pytest_bazel.main()
