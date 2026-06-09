from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pygit2
import pytest_bazel

from finance.augur.ingest.fetch import commit_and_push, run_scrape, write_sources
from finance.augur.ingest.http_fetch import HttpGet
from finance.evidence.sources import EvidenceKind, EvidenceSource

SOURCE = EvidenceSource(
    kind=EvidenceKind.FRED,
    series_id="CPIAUCSL",
    upstream_url="https://example.test/x.csv",
    output_filename="fred_cpi_us.csv",
)
NOW = datetime(2026, 6, 9, tzinfo=UTC)
_SIG = pygit2.Signature("t", "t@t.test")


def _constant_get(body: bytes) -> HttpGet:
    async def get(url: str, user_agent: str) -> bytes:
        return body

    return get


def _commit_all(repo: pygit2.Repository, message: str, *, push: bool = False) -> None:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    repo.create_commit("refs/heads/main", _SIG, _SIG, message, tree, parents)
    if push:
        repo.remotes["origin"].push(["refs/heads/main"])


def _seed_remote(remote: Path) -> None:
    """Create a bare remote on `main` with one seed commit, ready for a --branch main clone."""
    pygit2.init_repository(str(remote), bare=True, initial_head="main")
    work = remote.parent / f"{remote.name}-seed"
    repo = pygit2.init_repository(str(work), initial_head="main")
    (work / ".keep").write_text("")
    repo.remotes.create("origin", str(remote))
    _commit_all(repo, "init", push=True)


def _remote_blob(remote: Path, path: str) -> bytes:
    return pygit2.Repository(str(remote)).revparse_single(f"main:{path}").peel(pygit2.Blob).data


def _remote_last_message(remote: Path) -> str:
    return pygit2.Repository(str(remote)).revparse_single("main").peel(pygit2.Commit).message


async def test_write_sources_writes_each_file_by_output_filename(tmp_path: Path) -> None:
    failures = await write_sources(tmp_path, [SOURCE], http_get=_constant_get(b"payload"))
    assert failures == 0
    assert (tmp_path / "fred_cpi_us.csv").read_bytes() == b"payload"


async def test_write_sources_counts_upstream_failures_and_keeps_going(tmp_path: Path) -> None:
    other = EvidenceSource(
        kind=EvidenceKind.YAHOO, series_id="SPY", upstream_url="https://example.test/spy", output_filename="spy.json"
    )

    async def get(url: str, user_agent: str) -> bytes:
        if url == SOURCE.upstream_url:
            raise httpx.ConnectError("upstream down")
        return b"ok"

    failures = await write_sources(tmp_path, [SOURCE, other], http_get=get)
    assert failures == 1
    # The failed source wrote no file; the healthy one still did.
    assert not (tmp_path / "fred_cpi_us.csv").exists()
    assert (tmp_path / "spy.json").read_bytes() == b"ok"


async def test_write_sources_fetches_concurrently(tmp_path: Path) -> None:
    # All sources must be in flight at once for the barrier to release; a serial fetch
    # would block on the first party forever, so the timeout below would trip the test.
    sources = [
        EvidenceSource(
            kind=EvidenceKind.FRED,
            series_id=f"S{i}",
            upstream_url=f"https://example.test/{i}",
            output_filename=f"{i}.csv",
        )
        for i in range(4)
    ]
    barrier = asyncio.Barrier(len(sources))

    async def get(url: str, user_agent: str) -> bytes:
        async with asyncio.timeout(10):
            await barrier.wait()
        return b"ok"

    failures = await write_sources(tmp_path, sources, http_get=get)
    assert failures == 0
    assert all((tmp_path / s.output_filename).read_bytes() == b"ok" for s in sources)


def test_commit_and_push_skips_when_nothing_changed(tmp_path: Path) -> None:
    repo = pygit2.init_repository(str(tmp_path / "work"), initial_head="main")
    (Path(repo.workdir) / ".keep").write_text("")
    _commit_all(repo, "init")
    # No new content staged -> no commit.
    assert commit_and_push(repo, "main", now=NOW) is False


def test_commit_and_push_commits_changed_files(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    _seed_remote(remote)
    work = tmp_path / "work"
    repo = pygit2.clone_repository(str(remote), str(work), checkout_branch="main")

    (work / "fred_cpi_us.csv").write_text("new evidence")
    assert commit_and_push(repo, "main", now=NOW) is True
    # The pushed commit message carries the UTC date.
    assert "evidence: refresh 2026-06-09" in _remote_last_message(remote)


async def test_run_scrape_clones_writes_and_pushes(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    _seed_remote(remote)

    # depth=0 (full clone): libgit2's local file transport doesn't support shallow fetch.
    code = await run_scrape(
        str(remote), "main", [SOURCE], username="", password="", http_get=_constant_get(b"body"), now=NOW, depth=0
    )
    assert code == 0
    # The fetched file is now committed on the remote's main.
    assert _remote_blob(remote, "fred_cpi_us.csv") == b"body"


async def test_run_scrape_returns_nonzero_when_a_source_fails(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    _seed_remote(remote)

    async def boom(url: str, user_agent: str) -> bytes:
        raise httpx.ConnectError("upstream down")

    assert (
        await run_scrape(str(remote), "main", [SOURCE], username="", password="", http_get=boom, now=NOW, depth=0) == 1
    )


if __name__ == "__main__":
    pytest_bazel.main()
