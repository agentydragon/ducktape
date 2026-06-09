from __future__ import annotations

from pathlib import Path

import pytest
import pytest_bazel

from loom.gym.evidence_checkout import ensure_checkout


def test_env_dir_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AUGUR_EVIDENCE_DIR", str(tmp_path))
    assert ensure_checkout() == tmp_path


def test_missing_credentials_explain_where_to_get_them(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AUGUR_EVIDENCE_DIR", raising=False)
    monkeypatch.delenv("AUGUR_EVIDENCE_GIT_USERNAME", raising=False)
    monkeypatch.delenv("AUGUR_EVIDENCE_GIT_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="augur-evidence-git-read"):
        ensure_checkout()


if __name__ == "__main__":
    pytest_bazel.main()
