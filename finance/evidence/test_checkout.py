from __future__ import annotations

from pathlib import Path

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


if __name__ == "__main__":
    pytest_bazel.main()
