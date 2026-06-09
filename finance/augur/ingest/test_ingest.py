from __future__ import annotations

import subprocess
import urllib.error
from datetime import UTC, datetime
from pathlib import Path

import pytest_bazel

from finance.augur.ingest.evidence_sources import EvidenceKind, EvidenceSource
from finance.augur.ingest.fetch import _authenticated_url, commit_and_push, run_scrape, write_sources
from finance.augur.ingest.http_fetch import HttpGet

SOURCE = EvidenceSource(
    kind=EvidenceKind.FRED,
    series_id="CPIAUCSL",
    upstream_url="https://example.test/x.csv",
    output_filename="fred_cpi_us.csv",
)
NOW = datetime(2026, 6, 9, tzinfo=UTC)


def _constant_get(body: bytes) -> HttpGet:
    def get(url: str, user_agent: str) -> bytes:
        return body

    return get


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "t@t.test")
    _git(path, "config", "user.name", "t")
    (path / ".keep").write_text("")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "init")


def test_authenticated_url_injects_credentials() -> None:
    url = _authenticated_url(
        "http://forgejo-http.forgejo:3000/augur-evidence/augur-evidence.git", username="u", password="p@ss"
    )
    # password is percent-encoded; host + port + path preserved.
    assert url == "http://u:p%40ss@forgejo-http.forgejo:3000/augur-evidence/augur-evidence.git"


def test_write_sources_writes_each_file_by_output_filename(tmp_path: Path) -> None:
    failures = write_sources(tmp_path, [SOURCE], http_get=_constant_get(b"payload"))
    assert failures == 0
    assert (tmp_path / "fred_cpi_us.csv").read_bytes() == b"payload"


def test_write_sources_counts_upstream_failures_and_keeps_going(tmp_path: Path) -> None:
    other = EvidenceSource(
        kind=EvidenceKind.YAHOO, series_id="SPY", upstream_url="https://example.test/spy", output_filename="spy.json"
    )

    def get(url: str, user_agent: str) -> bytes:
        if url == SOURCE.upstream_url:
            raise urllib.error.URLError("upstream down")
        return b"ok"

    failures = write_sources(tmp_path, [SOURCE, other], http_get=get)
    assert failures == 1
    # The failed source wrote no file; the healthy one still did.
    assert not (tmp_path / "fred_cpi_us.csv").exists()
    assert (tmp_path / "spy.json").read_bytes() == b"ok"


def test_commit_and_push_skips_when_nothing_changed(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    # No new content staged -> no commit.
    assert commit_and_push(tmp_path, "main", now=NOW) is False


def test_commit_and_push_commits_changed_files(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "-q", "--bare", "-b", "main")
    work = tmp_path / "work"
    _git(tmp_path, "clone", "-q", str(remote), str(work))
    _git(work, "config", "user.email", "t@t.test")
    _git(work, "config", "user.name", "t")
    (work / "seed").write_text("")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "seed")
    _git(work, "push", "-q", "origin", "main")

    (work / "fred_cpi_us.csv").write_text("new evidence")
    assert commit_and_push(work, "main", now=NOW) is True
    # The pushed commit message carries the UTC date.
    assert "evidence: refresh 2026-06-09" in _git(remote, "log", "--format=%s", "-1")


def test_run_scrape_clones_writes_and_pushes(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "-q", "--bare", "-b", "main")
    # Seed the bare remote with an initial commit on main so a --depth 1 clone has a branch.
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(remote), str(seed))
    _git(seed, "config", "user.email", "t@t.test")
    _git(seed, "config", "user.name", "t")
    (seed / ".keep").write_text("")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "push", "-q", "origin", "main")

    code = run_scrape(str(remote), "main", [SOURCE], username="", password="", http_get=_constant_get(b"body"), now=NOW)
    assert code == 0
    # The fetched file is now committed on the remote's main.
    assert _git(remote, "show", "main:fred_cpi_us.csv") == "body"


def test_run_scrape_returns_nonzero_when_a_source_fails(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "-q", "--bare", "-b", "main")
    seed = tmp_path / "seed"
    _git(tmp_path, "clone", "-q", str(remote), str(seed))
    _git(seed, "config", "user.email", "t@t.test")
    _git(seed, "config", "user.name", "t")
    (seed / ".keep").write_text("")
    _git(seed, "add", "-A")
    _git(seed, "commit", "-q", "-m", "init")
    _git(seed, "push", "-q", "origin", "main")

    def boom(url: str, user_agent: str) -> bytes:
        raise urllib.error.URLError("upstream down")

    assert run_scrape(str(remote), "main", [SOURCE], username="", password="", http_get=boom, now=NOW) == 1


if __name__ == "__main__":
    pytest_bazel.main()
