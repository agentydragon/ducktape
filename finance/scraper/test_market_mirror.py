from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import pytest_bazel

from finance.evidence.markets import MarketEntry, Platform, bets_jsonl_path, comments_jsonl_path, market_json_path
from finance.scraper.http_fetch import HttpGet
from finance.scraper.market_mirror import GAMMA_API, KALSHI_API, MANIFOLD_API, RequestPacer, sync_markets

DEEP = MarketEntry(platform=Platform.MANIFOLD, market_id="m1", deep=True)
MARKET_URL = f"{MANIFOLD_API}/market/m1"
BETS_URL = f"{MANIFOLD_API}/bets?contractId=m1&limit=1000"
COMMENTS_URL = f"{MANIFOLD_API}/comments?contractId=m1&limit=1000&page=0"

NO_PACING = RequestPacer(0)


def _bet(bet_id: str, created_time: int) -> dict[str, object]:
    # Distinctive non-alphabetical key order proves lines keep the provider's order.
    return {"id": bet_id, "probAfter": 0.5, "createdTime": created_time, "amount": 1.0}


def _jsonl(rows: list[dict[str, object]]) -> str:
    return "".join(json.dumps(row, separators=(",", ":")) + "\n" for row in rows)


def _dispatch(responses: dict[str, bytes | Exception]) -> tuple[HttpGet, list[str]]:
    calls: list[str] = []

    async def get(url: str, user_agent: str) -> bytes:
        calls.append(url)
        result = responses[url]
        if isinstance(result, Exception):
            raise result
        return result

    return get, calls


def _manifold_responses(bets_pages: dict[str, list[dict[str, object]]]) -> dict[str, bytes | Exception]:
    """market.json + empty comments + the given bets pages (keyed by full URL)."""
    responses: dict[str, bytes | Exception] = {MARKET_URL: b'{"id":"m1","probability":0.5}', COMMENTS_URL: b"[]"}
    responses.update({url: json.dumps(page).encode() for url, page in bets_pages.items()})
    return responses


async def test_deep_sync_writes_verbatim_market_and_ascending_bets(tmp_path: Path) -> None:
    # API serves bets newest-first; the file stores them ascending, lines verbatim
    # (provider key order, compact).
    get, _ = _dispatch(_manifold_responses({BETS_URL: [_bet("b2", 2000), _bet("b1", 1000)]}))
    failed = await sync_markets(tmp_path, [DEEP], http_get=get, pacer=NO_PACING)
    assert failed == set()
    assert market_json_path(tmp_path, Platform.MANIFOLD, "m1").read_bytes() == b'{"id":"m1","probability":0.5}'
    assert bets_jsonl_path(tmp_path, Platform.MANIFOLD, "m1").read_text() == _jsonl(
        [_bet("b1", 1000), _bet("b2", 2000)]
    )


async def test_incremental_bets_appends_only_the_gap(tmp_path: Path) -> None:
    bets_path = bets_jsonl_path(tmp_path, Platform.MANIFOLD, "m1")
    bets_path.parent.mkdir(parents=True)
    bets_path.write_text(_jsonl([_bet("b1", 1000), _bet("b2", 2000)]))

    # Page contains the stored newest bet (b2) mid-page; only b3+b4 are appended.
    get, calls = _dispatch(
        _manifold_responses({BETS_URL: [_bet("b4", 4000), _bet("b3", 3000), _bet("b2", 2000), _bet("b1", 1000)]})
    )
    failed = await sync_markets(tmp_path, [DEEP], http_get=get, pacer=NO_PACING)
    assert failed == set()
    assert bets_path.read_text() == _jsonl([_bet("b1", 1000), _bet("b2", 2000), _bet("b3", 3000), _bet("b4", 4000)])
    assert calls.count(BETS_URL) == 1  # stop at the stored id: no second page fetched


async def test_full_backfill_pages_until_short_page(tmp_path: Path) -> None:
    # 1000-bet page (exactly full) -> keep paging with before=<oldest>; short page ends it.
    first_page = [_bet(f"b{i}", 10_000 - i) for i in range(1000)]
    second_page = [_bet("old2", 20), _bet("old1", 10)]
    get, _ = _dispatch(_manifold_responses({BETS_URL: first_page, f"{BETS_URL}&before=b999": second_page}))
    failed = await sync_markets(tmp_path, [DEEP], http_get=get, pacer=NO_PACING)
    assert failed == set()
    lines = bets_jsonl_path(tmp_path, Platform.MANIFOLD, "m1").read_text().splitlines()
    assert len(lines) == 1002
    assert json.loads(lines[0])["id"] == "old1"
    assert json.loads(lines[-1])["id"] == "b0"


async def test_empty_bets_history_creates_empty_file(tmp_path: Path) -> None:
    get, _ = _dispatch(_manifold_responses({BETS_URL: []}))
    failed = await sync_markets(tmp_path, [DEEP], http_get=get, pacer=NO_PACING)
    assert failed == set()
    assert bets_jsonl_path(tmp_path, Platform.MANIFOLD, "m1").read_bytes() == b""


async def test_failed_bets_page_leaves_file_untouched(tmp_path: Path) -> None:
    bets_path = bets_jsonl_path(tmp_path, Platform.MANIFOLD, "m1")
    bets_path.parent.mkdir(parents=True)
    before = _jsonl([_bet("b1", 1000)])
    bets_path.write_text(before)

    # The whole gap is appended atomically: a failed page mid-backfill must not
    # write a partial gap (the next run would otherwise never close the hole).
    first_page = [_bet(f"b{i}", 10_000 - i) for i in range(2, 1002)]
    responses = _manifold_responses({BETS_URL: first_page})
    responses[f"{BETS_URL}&before=b1001"] = httpx.ConnectError("upstream down")
    get, _ = _dispatch(responses)
    failed = await sync_markets(tmp_path, [DEEP], http_get=get, pacer=NO_PACING)
    assert failed == {DEEP}
    assert bets_path.read_text() == before


async def test_comments_rewrite_sorts_and_drops_deleted(tmp_path: Path) -> None:
    comments_path = comments_jsonl_path(tmp_path, Platform.MANIFOLD, "m1")
    comments_path.parent.mkdir(parents=True)
    deleted = {"id": "gone", "createdTime": 1500}
    comments_path.write_text(_jsonl([{"id": "c1", "createdTime": 1000}, deleted]))

    responses = _manifold_responses({BETS_URL: []})
    # Upstream serves newest-first and no longer includes the deleted comment.
    responses[COMMENTS_URL] = json.dumps(
        [{"id": "c2", "createdTime": 2000}, {"id": "c1", "createdTime": 1000}]
    ).encode()
    get, _ = _dispatch(responses)
    failed = await sync_markets(tmp_path, [DEEP], http_get=get, pacer=NO_PACING)
    assert failed == set()
    assert comments_path.read_text() == _jsonl([{"id": "c1", "createdTime": 1000}, {"id": "c2", "createdTime": 2000}])


async def test_kalshi_snapshot_stored_verbatim(tmp_path: Path) -> None:
    entry = MarketEntry(platform=Platform.KALSHI, market_id="KXT-1")
    body = b'{"market": {"ticker": "KXT-1", "yes_bid_dollars": "0.1200"}}'
    get, _ = _dispatch({f"{KALSHI_API}/markets/KXT-1": body})
    failed = await sync_markets(tmp_path, [entry], http_get=get, pacer=NO_PACING)
    assert failed == set()
    assert market_json_path(tmp_path, Platform.KALSHI, "KXT-1").read_bytes() == body


async def test_polymarket_numeric_id_uses_path_endpoint(tmp_path: Path) -> None:
    entry = MarketEntry(platform=Platform.POLYMARKET, market_id="253591")
    body = b'{"id": "253591", "bestBid": 0.4}'
    get, _ = _dispatch({f"{GAMMA_API}/markets/253591": body})
    failed = await sync_markets(tmp_path, [entry], http_get=get, pacer=NO_PACING)
    assert failed == set()
    assert market_json_path(tmp_path, Platform.POLYMARKET, "253591").read_bytes() == body


async def test_polymarket_condition_id_retries_closed_and_stores_raw_list(tmp_path: Path) -> None:
    # Gamma's condition-id query filters by state: open markets only on the bare
    # query, closed ones only with closed=true. The closed fallback's raw list body
    # is stored as served.
    entry = MarketEntry(platform=Platform.POLYMARKET, market_id="0xabc")
    closed_body = b'[{"id": "1", "conditionId": "0xabc", "closed": true}]'
    get, calls = _dispatch(
        {
            f"{GAMMA_API}/markets?condition_ids=0xabc": b"[]",
            f"{GAMMA_API}/markets?condition_ids=0xabc&closed=true": closed_body,
        }
    )
    failed = await sync_markets(tmp_path, [entry], http_get=get, pacer=NO_PACING)
    assert failed == set()
    assert market_json_path(tmp_path, Platform.POLYMARKET, "0xabc").read_bytes() == closed_body
    assert len(calls) == 2


async def test_polymarket_condition_id_unknown_everywhere_fails_entry(tmp_path: Path) -> None:
    entry = MarketEntry(platform=Platform.POLYMARKET, market_id="0xdead")
    get, _ = _dispatch(
        {
            f"{GAMMA_API}/markets?condition_ids=0xdead": b"[]",
            f"{GAMMA_API}/markets?condition_ids=0xdead&closed=true": b"[]",
        }
    )
    failed = await sync_markets(tmp_path, [entry], http_get=get, pacer=NO_PACING)
    assert failed == {entry}
    assert not market_json_path(tmp_path, Platform.POLYMARKET, "0xdead").exists()


async def test_one_failed_market_does_not_block_the_rest(tmp_path: Path) -> None:
    healthy = MarketEntry(platform=Platform.KALSHI, market_id="KXT-1")
    broken = MarketEntry(platform=Platform.KALSHI, market_id="KXT-2")
    responses: dict[str, bytes | Exception] = {
        f"{KALSHI_API}/markets/KXT-1": b'{"market": {}}',
        f"{KALSHI_API}/markets/KXT-2": httpx.ConnectError("flap"),
    }
    get, _ = _dispatch(responses)
    failed = await sync_markets(tmp_path, [broken, healthy], http_get=get, pacer=NO_PACING)
    assert failed == {broken}
    assert market_json_path(tmp_path, Platform.KALSHI, "KXT-1").exists()


async def test_resync_with_identical_payloads_is_byte_stable(tmp_path: Path) -> None:
    get, _ = _dispatch(_manifold_responses({BETS_URL: [_bet("b1", 1000)]}))
    await sync_markets(tmp_path, [DEEP], http_get=get, pacer=NO_PACING)
    first = {
        path: path.read_bytes()
        for path in (
            market_json_path(tmp_path, Platform.MANIFOLD, "m1"),
            bets_jsonl_path(tmp_path, Platform.MANIFOLD, "m1"),
            comments_jsonl_path(tmp_path, Platform.MANIFOLD, "m1"),
        )
    }
    await sync_markets(tmp_path, [DEEP], http_get=get, pacer=NO_PACING)
    assert {path: path.read_bytes() for path in first} == first


async def test_request_pacer_spaces_requests() -> None:
    clock_now = 0.0
    sleeps: list[float] = []

    def clock() -> float:
        return clock_now

    async def sleep(seconds: float) -> None:
        nonlocal clock_now
        sleeps.append(seconds)
        clock_now += seconds

    pacer = RequestPacer(0.2, clock=clock, sleep=sleep)
    for _ in range(3):
        await pacer.wait()
    assert sleeps == pytest.approx([0.2, 0.2])


if __name__ == "__main__":
    pytest_bazel.main()
