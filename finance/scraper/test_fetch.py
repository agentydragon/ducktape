from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pygit2
import pytest
import pytest_bazel

from finance.evidence.markets import MarketEntry, Platform
from finance.evidence.sources import EVIDENCE_SOURCES, EvidenceKind, EvidenceSource
from finance.scraper.fetch import EVIDENCE_META_FILENAME, EvidenceManifest, commit_and_push, run_scrape, write_sources
from finance.scraper.http_fetch import HttpGet, _ca_file
from finance.scraper.market_mirror import KALSHI_API, MANIFOLD_API

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


async def _boom(url: str, user_agent: str) -> bytes:
    raise httpx.ConnectError("upstream down")


def _commit_all(repo: pygit2.Repository, message: str, *, push: bool = False) -> None:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [] if repo.head_is_unborn else [repo.head.target]
    repo.create_commit("refs/heads/main", _SIG, _SIG, message, tree, parents)
    if push:
        repo.remotes["origin"].push(["refs/heads/main"])


def _seed_remote(remote: Path, files: dict[str, str] | None = None) -> None:
    """Create a bare remote on `main` with one seed commit, ready for a --branch main clone."""
    pygit2.init_repository(str(remote), bare=True, initial_head="main")
    work = remote.parent / f"{remote.name}-seed"
    repo = pygit2.init_repository(str(work), initial_head="main")
    (work / ".keep").write_text("")
    for name, content in (files or {}).items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(content)
    repo.remotes.create("origin", str(remote))
    _commit_all(repo, "init", push=True)


def _seed_with_last_fetched(remote: Path, last_fetched: dict[str, datetime]) -> None:
    manifest = EvidenceManifest(last_fetched=last_fetched).model_dump_json()
    _seed_remote(remote, {EVIDENCE_META_FILENAME: manifest})


def _remote_blob(remote: Path, path: str) -> bytes:
    return pygit2.Repository(str(remote)).revparse_single(f"main:{path}").peel(pygit2.Blob).data


def _remote_last_message(remote: Path) -> str:
    return pygit2.Repository(str(remote)).revparse_single("main").peel(pygit2.Commit).message


def _remote_commit_count(remote: Path) -> int:
    repo = pygit2.Repository(str(remote))
    return sum(1 for _ in repo.walk(repo.revparse_single("main").peel(pygit2.Commit).id))


def test_ca_file_honors_ssl_cert_file(monkeypatch: pytest.MonkeyPatch) -> None:
    # Agent sandboxes inject a TLS-intercepting proxy's CA via SSL_CERT_FILE; the
    # fetcher must trust it there while staying on certifi when the env is unset.
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/proxy-ca.pem")
    assert _ca_file() == "/tmp/proxy-ca.pem"
    monkeypatch.delenv("SSL_CERT_FILE")
    assert _ca_file().endswith("cacert.pem")


async def test_write_sources_reports_failed_sources_and_keeps_going(tmp_path: Path) -> None:
    other = EvidenceSource(
        kind=EvidenceKind.YAHOO, series_id="SPY", upstream_url="https://example.test/spy", output_filename="spy.json"
    )

    async def get(url: str, user_agent: str) -> bytes:
        if url == SOURCE.upstream_url:
            raise httpx.ConnectError("upstream down")
        return b"ok"

    failed = await write_sources(tmp_path, [SOURCE, other], http_get=get)
    assert failed == {SOURCE}
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

    failed = await write_sources(tmp_path, sources, http_get=get)
    assert failed == set()
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
    # The fetched file is now committed on the remote's main, with the fetch recorded in the manifest.
    assert _remote_blob(remote, "fred_cpi_us.csv") == b"body"
    manifest = EvidenceManifest.model_validate_json(_remote_blob(remote, EVIDENCE_META_FILENAME))
    assert manifest.last_fetched[SOURCE.provenance_label] == NOW


async def test_run_scrape_fails_on_any_miss_without_stale_policy(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    _seed_remote(remote)
    # No stale_after -> any failed source fails the Job.
    assert (
        await run_scrape(str(remote), "main", [SOURCE], username="", password="", http_get=_boom, now=NOW, depth=0) == 1
    )


@pytest.mark.parametrize(
    ("fetched_hours", "max_age_hours", "http_get", "expected_code", "expected_blob", "expect_skip"),
    [
        pytest.param(1, 20, _constant_get(b"new"), 0, None, True, id="fresh-source-skip"),
        pytest.param(48, 20, _constant_get(b"body"), 0, b"body", False, id="old-source-refetch"),
        pytest.param(1, None, _boom, 0, None, False, id="failed-fetch-with-fresh-copy"),
        pytest.param(100, None, _boom, 1, None, False, id="failed-fetch-with-stale-copy"),
    ],
)
async def test_run_scrape_source_freshness(
    tmp_path: Path,
    fetched_hours: int,
    max_age_hours: int | None,
    http_get: HttpGet,
    expected_code: int,
    expected_blob: bytes | None,
    expect_skip: bool,
) -> None:
    remote = tmp_path / "remote.git"
    _seed_with_last_fetched(remote, {SOURCE.provenance_label: NOW - timedelta(hours=fetched_hours)})
    calls: list[str] = []

    async def track_get(url: str, user_agent: str) -> bytes:
        calls.append(url)
        return await http_get(url, user_agent)

    code = await run_scrape(
        str(remote),
        "main",
        [SOURCE],
        username="",
        password="",
        http_get=track_get,
        now=NOW,
        depth=0,
        max_age=timedelta(hours=max_age_hours) if max_age_hours is not None else None,
        stale_after=timedelta(hours=72),
    )
    assert code == expected_code
    if expect_skip:
        assert not calls
    if expected_blob is not None:
        assert _remote_blob(remote, "fred_cpi_us.csv") == expected_blob


# ── market mirror pass ──────────────────────────────────────────────────

MANIFOLD_ENTRY = MarketEntry(platform=Platform.MANIFOLD, market_id="m1", deep=True)
KALSHI_ENTRY = MarketEntry(platform=Platform.KALSHI, market_id="KXT-1")
_BETS_URL = f"{MANIFOLD_API}/bets?contractId=m1&limit=1000"
_MARKET_RESPONSES: dict[str, bytes] = {
    f"{MANIFOLD_API}/market/m1": b'{"id":"m1","probability":0.5}',
    _BETS_URL: b'[{"id":"b2","createdTime":2000},{"id":"b1","createdTime":1000}]',
    f"{MANIFOLD_API}/comments?contractId=m1&limit=1000&page=0": b"[]",
    f"{KALSHI_API}/markets/KXT-1": b'{"market":{"ticker":"KXT-1"}}',
}


def _dispatch_get(responses: dict[str, bytes]) -> tuple[HttpGet, list[str]]:
    calls: list[str] = []

    async def get(url: str, user_agent: str) -> bytes:
        calls.append(url)
        return responses[url]

    return get, calls


async def test_run_scrape_mirrors_markets_and_records_manifest(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    _seed_remote(remote)
    get, _ = _dispatch_get({SOURCE.upstream_url: b"body", **_MARKET_RESPONSES})

    code = await run_scrape(
        str(remote),
        "main",
        [SOURCE],
        username="",
        password="",
        http_get=get,
        now=NOW,
        depth=0,
        markets=[MANIFOLD_ENTRY, KALSHI_ENTRY],
    )
    assert code == 0
    assert _remote_blob(remote, "markets/manifold/m1/market.json") == b'{"id":"m1","probability":0.5}'
    assert _remote_blob(remote, "markets/manifold/m1/bets.jsonl") == (
        b'{"id":"b1","createdTime":1000}\n{"id":"b2","createdTime":2000}\n'
    )
    assert _remote_blob(remote, "markets/manifold/m1/comments.jsonl") == b""
    assert _remote_blob(remote, "markets/kalshi/KXT-1/market.json") == b'{"market":{"ticker":"KXT-1"}}'
    manifest = EvidenceManifest.model_validate_json(_remote_blob(remote, EVIDENCE_META_FILENAME))
    assert manifest.last_fetched[MANIFOLD_ENTRY.provenance_label] == NOW
    assert manifest.last_fetched[KALSHI_ENTRY.provenance_label] == NOW


async def test_run_scrape_appends_bet_gap_to_seeded_history(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"
    _seed_remote(remote, {"markets/manifold/m1/bets.jsonl": '{"id":"b1","createdTime":1000}\n'})
    get, _ = _dispatch_get({SOURCE.upstream_url: b"body", **_MARKET_RESPONSES})

    code = await run_scrape(
        str(remote),
        "main",
        [SOURCE],
        username="",
        password="",
        http_get=get,
        now=NOW,
        depth=0,
        markets=[MANIFOLD_ENTRY],
    )
    assert code == 0
    # The API page held b2 (new) then b1 (already stored): exactly one line appended.
    assert _remote_blob(remote, "markets/manifold/m1/bets.jsonl") == (
        b'{"id":"b1","createdTime":1000}\n{"id":"b2","createdTime":2000}\n'
    )


async def test_run_scrape_unchanged_markets_skip_the_commit(tmp_path: Path) -> None:
    # A fully-synced (e.g. resolved) market converges to a no-op: identical payloads
    # produce an identical tree, so the second run pushes nothing.
    remote = tmp_path / "remote.git"
    _seed_remote(remote)
    get, _ = _dispatch_get({SOURCE.upstream_url: b"body", **_MARKET_RESPONSES})

    async def scrape_once(now: datetime) -> int:
        return await run_scrape(
            str(remote),
            "main",
            [SOURCE],
            username="",
            password="",
            http_get=get,
            now=now,
            depth=0,
            markets=[MANIFOLD_ENTRY, KALSHI_ENTRY],
        )

    assert await scrape_once(NOW) == 0
    count_after_first = _remote_commit_count(remote)
    # An hour later (the hourly-cron case): identical payloads and a manifest stamp
    # young enough to keep — nothing to commit, not even a manifest-only diff.
    assert await scrape_once(NOW + timedelta(hours=1)) == 0
    assert _remote_commit_count(remote) == count_after_first


async def test_run_scrape_restamps_manifest_after_refresh_floor(tmp_path: Path) -> None:
    # Quiet markets skip the manifest bump (no churn), but the stamp still refreshes
    # about daily so the stale-after exit stays honest about ongoing successes.
    remote = tmp_path / "remote.git"
    _seed_with_last_fetched(remote, {KALSHI_ENTRY.provenance_label: NOW - timedelta(hours=21)})
    get, _ = _dispatch_get(_MARKET_RESPONSES)
    code = await run_scrape(
        str(remote), "main", [], username="", password="", http_get=get, now=NOW, depth=0, markets=[KALSHI_ENTRY]
    )
    assert code == 0
    manifest = EvidenceManifest.model_validate_json(_remote_blob(remote, EVIDENCE_META_FILENAME))
    assert manifest.last_fetched[KALSHI_ENTRY.provenance_label] == NOW


@pytest.mark.parametrize(
    ("fetched_hours", "market_max_age_hours", "fail", "expected_code", "expected_calls"),
    [
        pytest.param((1, 1), None, False, 0, (False, True), id="source-skip-market-refresh"),
        pytest.param((None, 1), 6, False, 0, (None, False), id="fresh-market-skip"),
        pytest.param((None, 1), None, True, 0, (None, None), id="failed-fresh-market"),
        pytest.param((None, 100), None, True, 1, (None, None), id="failed-stale-market"),
    ],
)
async def test_run_scrape_market_freshness(
    tmp_path: Path,
    fetched_hours: tuple[int | None, int],
    market_max_age_hours: int | None,
    fail: bool,
    expected_code: int,
    expected_calls: tuple[bool | None, bool | None],
) -> None:
    remote = tmp_path / "remote.git"
    source_hours, market_hours = fetched_hours
    last_fetched = {KALSHI_ENTRY.provenance_label: NOW - timedelta(hours=market_hours)}
    if source_hours is not None:
        last_fetched[SOURCE.provenance_label] = NOW - timedelta(hours=source_hours)
    _seed_with_last_fetched(remote, last_fetched)
    sources = [] if fail else [SOURCE]
    get: HttpGet
    calls: list[str]
    if fail:
        get, calls = _boom, []
    else:
        get, calls = _dispatch_get({SOURCE.upstream_url: b"body", **_MARKET_RESPONSES})

    code = await run_scrape(
        str(remote),
        "main",
        sources,
        username="",
        password="",
        http_get=get,
        now=NOW,
        depth=0,
        markets=[KALSHI_ENTRY],
        max_age=timedelta(hours=20),
        market_max_age=timedelta(hours=market_max_age_hours) if market_max_age_hours is not None else None,
        stale_after=timedelta(hours=72),
    )
    assert code == expected_code
    source_called, market_called = expected_calls
    if source_called is not None:
        assert (SOURCE.upstream_url in calls) is source_called
    if market_called is not None:
        assert (f"{KALSHI_API}/markets/KXT-1" in calls) is market_called


if __name__ == "__main__":
    pytest_bazel.main()


def test_every_declared_source_is_fetched_and_written_verbatim(tmp_path: Path) -> None:
    """The scraper is format-agnostic on purpose: it writes response bytes under
    `output_filename` and never parses. This pins that for EVERY source in the spec, including
    binary ones — Ken French ships a zip, and a scraper that assumed text would corrupt it in a
    way only the loader would notice, one CronJob cycle later."""

    bodies = {source.upstream_url: bytes([0, 159, 146, 150]) + source.series_id.encode() for source in EVIDENCE_SOURCES}

    async def fake_get(url: str, user_agent: str) -> bytes:
        return bodies[url]

    asyncio.run(write_sources(tmp_path, EVIDENCE_SOURCES, http_get=fake_get))

    for source in EVIDENCE_SOURCES:
        assert (tmp_path / source.output_filename).read_bytes() == bodies[source.upstream_url]
