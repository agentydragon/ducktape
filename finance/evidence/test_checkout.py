from __future__ import annotations

from pathlib import Path

import pygit2
import pytest
import pytest_bazel

from finance.evidence.checkout import _EgressTrust, ensure_checkout


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


def test_certificate_check_accepts_libgit2_valid_cert() -> None:
    # Never reject what libgit2 itself trusts.
    assert _EgressTrust().certificate_check(None, True, b"git.allegedly.works") is True


def test_certificate_check_accepts_mitm_cert_behind_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    # libgit2 marks the cluster mitmproxy's MITM cert invalid; accept it because
    # the only egress is that trusted in-cluster proxy.
    monkeypatch.setenv("HTTPS_PROXY", "http://mitmproxy.agents-mitmproxy.svc.cluster.local:8080")
    assert _EgressTrust().certificate_check(None, False, b"git.allegedly.works") is True


def test_certificate_check_rejects_invalid_cert_without_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    # Outside a proxied egress, an invalid cert is a real failure — don't mask it.
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    assert _EgressTrust().certificate_check(None, False, b"git.allegedly.works") is False


def test_clone_uses_egress_trust_callbacks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _clear_evidence_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AUGUR_EVIDENCE_GIT_USERNAME", "u")
    monkeypatch.setenv("AUGUR_EVIDENCE_GIT_PASSWORD", "p")
    captured: dict[str, object] = {}

    def fake_clone(url: str, path: str, *, callbacks: object, **_: object) -> None:
        captured["callbacks"] = callbacks
        (Path(path) / ".git").mkdir(parents=True)

    monkeypatch.setattr(pygit2, "clone_repository", fake_clone)
    ensure_checkout()
    assert isinstance(captured["callbacks"], _EgressTrust)


if __name__ == "__main__":
    pytest_bazel.main()
