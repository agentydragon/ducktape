"""Evidence snapshots identify and retain exactly the source bytes an operation uses."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pygit2
import pytest
import pytest_bazel

from finance.evidence.snapshot import EvidenceSnapshot
from finance.evidence.sources import FRED_CPI, FRED_GS10


def test_plain_directory_snapshot_captures_all_sources_before_later_reads(tmp_path: Path) -> None:
    (tmp_path / FRED_CPI.output_filename).write_bytes(b"original cpi")
    (tmp_path / FRED_GS10.output_filename).write_bytes(b"original rate")
    snapshot = EvidenceSnapshot.capture(tmp_path, (FRED_CPI, FRED_GS10))

    (tmp_path / FRED_CPI.output_filename).write_bytes(b"changed")
    (tmp_path / FRED_GS10.output_filename).unlink()
    assert snapshot.source_bytes(FRED_CPI) == b"original cpi"
    assert snapshot.source_bytes(FRED_GS10) == b"original rate"
    assert snapshot.revision is None
    assert snapshot.source_hashes() == {
        FRED_CPI.provenance_label: hashlib.sha256(b"original cpi").hexdigest(),
        FRED_GS10.provenance_label: hashlib.sha256(b"original rate").hexdigest(),
    }


def test_git_snapshot_is_pinned_to_commit_and_rejects_dirty_evidence(tmp_path: Path) -> None:
    repo = pygit2.init_repository(str(tmp_path), initial_head="main")
    (tmp_path / FRED_CPI.output_filename).write_bytes(b"committed cpi")
    repo.index.add(FRED_CPI.output_filename)
    repo.index.write()
    author = pygit2.Signature("Test", "test@example.org")
    commit = repo.create_commit("HEAD", author, author, "initial evidence", repo.index.write_tree(), [])

    snapshot = EvidenceSnapshot.capture(tmp_path, (FRED_CPI,))
    assert snapshot.revision == str(commit)
    assert snapshot.source_bytes(FRED_CPI) == b"committed cpi"
    (tmp_path / FRED_CPI.output_filename).write_bytes(b"dirty")
    assert snapshot.source_bytes(FRED_CPI) == b"committed cpi"
    with pytest.raises(ValueError, match="differs from evidence revision"):
        EvidenceSnapshot.capture(tmp_path, (FRED_CPI,))


def test_snapshot_rejects_missing_or_undeclared_sources(tmp_path: Path) -> None:
    (tmp_path / FRED_CPI.output_filename).write_bytes(b"cpi")
    with pytest.raises(RuntimeError, match="evidence not found"):
        EvidenceSnapshot.capture(tmp_path, (FRED_CPI, FRED_GS10))
    snapshot = EvidenceSnapshot.capture(tmp_path, (FRED_CPI,))
    with pytest.raises(KeyError):
        snapshot.source_bytes(FRED_GS10)


if __name__ == "__main__":
    pytest_bazel.main()
