from __future__ import annotations

from pathlib import Path

import pygit2
import pytest
import pytest_bazel

from finance.evidence.checkout import ensure_checkout


def test_env_dir_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AUGUR_EVIDENCE_DIR", str(tmp_path))
    assert ensure_checkout() == tmp_path


def _clear_evidence_env(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    monkeypatch.setenv("HOME", str(home))  # so Path.home()/.cache/... is empty
    monkeypatch.delenv("AUGUR_EVIDENCE_DIR", raising=False)
    monkeypatch.delenv("AUGUR_EVIDENCE_GIT_USERNAME", raising=False)
    monkeypatch.delenv("AUGUR_EVIDENCE_GIT_PASSWORD", raising=False)


def test_missing_credentials_explain_where_to_get_them(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_evidence_env(monkeypatch, tmp_path)
    with pytest.raises(RuntimeError, match="claude-forgejo-credentials"):
        ensure_checkout()


def test_cached_checkout_used_without_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # An existing clone is reused as-is — no creds needed on the warm path.
    _clear_evidence_env(monkeypatch, tmp_path)
    checkout = tmp_path / ".cache" / "loom" / "augur-evidence"
    (checkout / ".git").mkdir(parents=True)
    assert ensure_checkout() == checkout


class _RecordingSettings:
    ssl_cert_file: str | None = None


def _stub_clone(monkeypatch: pytest.MonkeyPatch) -> _RecordingSettings:
    """Replace pygit2's network clone + global settings so the cold path is hermetic.

    Returns the recording settings so callers can assert what libgit2 was pointed at.
    """
    settings = _RecordingSettings()
    monkeypatch.setattr(pygit2, "settings", settings)

    def fake_clone(url: str, path: str, **_: object) -> None:
        (Path(path) / ".git").mkdir(parents=True)

    monkeypatch.setattr(pygit2, "clone_repository", fake_clone)
    return settings


def test_clone_points_libgit2_at_egress_ca(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    # libgit2 ignores SSL_CERT_FILE, so the cold clone must mirror it into the
    # GIT_OPT_SET_SSL_CERT_LOCATIONS setting or the MITM egress cert is rejected.
    _clear_evidence_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AUGUR_EVIDENCE_GIT_USERNAME", "u")
    monkeypatch.setenv("AUGUR_EVIDENCE_GIT_PASSWORD", "p")
    ca = tmp_path / "mitmproxy-ca.pem"
    ca.write_text("-----BEGIN CERTIFICATE-----\n")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    settings = _stub_clone(monkeypatch)
    ensure_checkout()
    assert settings.ssl_cert_file == str(ca)


def test_clone_leaves_libgit2_default_trust_when_no_ca_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_evidence_env(monkeypatch, tmp_path)
    monkeypatch.delenv("GIT_SSL_CAINFO", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    monkeypatch.setenv("AUGUR_EVIDENCE_GIT_USERNAME", "u")
    monkeypatch.setenv("AUGUR_EVIDENCE_GIT_PASSWORD", "p")
    settings = _stub_clone(monkeypatch)
    ensure_checkout()
    assert settings.ssl_cert_file is None


if __name__ == "__main__":
    pytest_bazel.main()
