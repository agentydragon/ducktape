"""Mirror prediction-market state into the evidence repo working tree.

Per-market layout (paths from `finance.evidence.markets`):

  markets/manifold/<id>/market.json + bets.jsonl + comments.jsonl   (deep entries)
  markets/{polymarket,kalshi}/<id>/market.json                      (snapshot-only)

Data is stored in the form the platform returns it; consumers munge downstream.
`market.json` is the verbatim response body, overwritten each sync (git history = the
state timeline; an unchanged market produces an unchanged tree, so resolved markets
converge to no-op syncs). `bets.jsonl` is ascending `createdTime`, append-only: the
sync pages the API newest-first only until the newest stored bet id, then appends the
gap in one atomic write. `comments.jsonl` is fully rewritten each sync because
comments are editable/deletable upstream; lines are the raw API objects (provider
field order preserved) sorted ascending by (createdTime, id).

Known accepted limitation: Manifold mutates *maker* limit-order bet rows in place as
they fill; stored rows are never rewritten. Price reconstruction (`prob_at`) reads
`probAfter` of taker rows, which are immutable, so it is unaffected.

Polymarket id forms: a `0x...` condition id is fetched via `?condition_ids=` (the
response is a one-element list, stored as served). Gamma's state filter quirk —
verified live: the bare query excludes closed markets and `closed=true` excludes open
ones — makes that a two-step fetch: try open first, retry closed on an empty list.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path

from finance.evidence.markets import (
    MarketEntry,
    Platform,
    bets_jsonl_path,
    comments_jsonl_path,
    market_dir,
    market_json_path,
)
from finance.scraper.http_fetch import FETCH_ERRORS, HttpGet

logger = logging.getLogger(__name__)

MANIFOLD_API = "https://api.manifold.markets/v0"
GAMMA_API = "https://gamma-api.polymarket.com"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"

_USER_AGENT = "augur-evidence-scraper/1.0 (augur@allegedly.works)"
_PAGE_LIMIT = 1000


class MirrorDataError(Exception):
    """The platform returned an unusable shape (e.g. no market for a gamma query)."""


# Per-market failures callers treat as "log, skip, retry next run" — upstream fetch
# errors plus unusable payloads. Anything else is a bug and propagates.
MIRROR_ERRORS: tuple[type[BaseException], ...] = (*FETCH_ERRORS, MirrorDataError)

PacedGet = Callable[[str], Awaitable[bytes]]


class RequestPacer:
    """Global min-interval pacing across every mirror request.

    0.2s default ≈ 300 req/min — well under Manifold's documented 500 req/min/IP, and
    polite to the unmetered gamma/Kalshi endpoints. Clock + sleep are injectable so
    tests don't sleep.
    """

    def __init__(
        self,
        min_interval_seconds: float = 0.2,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._min_interval = min_interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._next_allowed = float("-inf")
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = self._clock()
            if (delay := self._next_allowed - now) > 0:
                await self._sleep(delay)
            self._next_allowed = max(now, self._next_allowed) + self._min_interval


async def sync_markets(
    workdir: Path, entries: Iterable[MarketEntry], *, http_get: HttpGet, pacer: RequestPacer | None = None
) -> set[MarketEntry]:
    """Sync every entry into `workdir`, sequentially (the pacer is the throughput bound).

    Per-market `MIRROR_ERRORS` are logged and collected, not raised — one dead market
    doesn't block mirroring the rest (mirrors `write_sources`). Returns the failed
    entries; the caller decides staleness via the scrape manifest.
    """
    pacer = pacer if pacer is not None else RequestPacer()

    async def paced_get(url: str) -> bytes:
        await pacer.wait()
        return await http_get(url, _USER_AGENT)

    failed: set[MarketEntry] = set()
    for entry in entries:
        try:
            await _sync_market(workdir, entry, paced_get=paced_get)
        except MIRROR_ERRORS:
            logger.warning("failed to sync %s", entry.provenance_label, exc_info=True)
            failed.add(entry)
        else:
            logger.info("synced %s", entry.provenance_label)
    return failed


async def _sync_market(workdir: Path, entry: MarketEntry, *, paced_get: PacedGet) -> None:
    body = await _fetch_snapshot(entry, paced_get=paced_get)
    market_dir(workdir, entry.platform, entry.market_id).mkdir(parents=True, exist_ok=True)
    market_json_path(workdir, entry.platform, entry.market_id).write_bytes(body)
    if entry.deep:
        await _sync_manifold_bets(workdir, entry, paced_get=paced_get)
        await _sync_manifold_comments(workdir, entry, paced_get=paced_get)


async def _fetch_snapshot(entry: MarketEntry, *, paced_get: PacedGet) -> bytes:
    """The market's current state, as the platform serves it."""
    match entry.platform:
        case Platform.MANIFOLD:
            return await paced_get(f"{MANIFOLD_API}/market/{entry.market_id}")
        case Platform.KALSHI:
            return await paced_get(f"{KALSHI_API}/markets/{entry.market_id}")
        case Platform.POLYMARKET:
            return await _fetch_gamma_snapshot(entry.market_id, paced_get=paced_get)


async def _fetch_gamma_snapshot(market_id: str, *, paced_get: PacedGet) -> bytes:
    if not market_id.startswith("0x"):
        return await paced_get(f"{GAMMA_API}/markets/{market_id}")
    # Condition-id queries return a JSON list filtered by market state (see module
    # docstring): open markets only without the flag, closed markets only with it.
    base = f"{GAMMA_API}/markets?condition_ids={market_id}"
    for url in (base, f"{base}&closed=true"):
        body = await paced_get(url)
        if json.loads(body):
            return body
    raise MirrorDataError(f"gamma returned no market for condition id {market_id!r}")


def _raw_jsonl_line(record: dict[str, object]) -> str:
    # Compact separators, no key sorting: the line is the API object as served,
    # modulo whitespace.
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"


def _newest_stored_bet_id(path: Path) -> str | None:
    """Bet id of the last line (the file is ascending), or None for absent/empty."""
    if not path.exists():
        return None
    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        return None
    bet_id = json.loads(lines[-1])["id"]
    if not isinstance(bet_id, str):
        raise MirrorDataError(f"stored bet id is not a string: {bet_id!r}")
    return bet_id


async def _sync_manifold_bets(workdir: Path, entry: MarketEntry, *, paced_get: PacedGet) -> None:
    path = bets_jsonl_path(workdir, entry.platform, entry.market_id)
    new_bets = await _fetch_bets_gap(entry.market_id, newest_stored_id=_newest_stored_bet_id(path), paced_get=paced_get)
    if not path.exists():
        path.touch()
    if new_bets:
        # One atomic append of the whole gap: a failed page fetch above leaves the
        # file (and the manifest, since the failure propagates) untouched, so the
        # next run re-fetches the same gap.
        with path.open("a", encoding="utf-8") as handle:
            handle.write("".join(_raw_jsonl_line(bet) for bet in new_bets))


async def _fetch_bets_gap(
    market_id: str, *, newest_stored_id: str | None, paced_get: PacedGet
) -> list[dict[str, object]]:
    """All bets newer than `newest_stored_id` (entire history when None), ascending.

    Pages `/bets` newest-first and stops at the first already-stored bet id — exact-id
    matching is robust to `createdTime` ties — or at a short page (history start).
    """
    collected: list[dict[str, object]] = []
    before: str | None = None
    while True:
        url = f"{MANIFOLD_API}/bets?contractId={market_id}&limit={_PAGE_LIMIT}"
        if before is not None:
            url += f"&before={before}"
        page = json.loads(await paced_get(url))
        if not isinstance(page, list):
            raise MirrorDataError(f"bets endpoint returned non-list for {market_id!r}")
        hit_stored = False
        for bet in page:
            if newest_stored_id is not None and bet["id"] == newest_stored_id:
                hit_stored = True
                break
            collected.append(bet)
        if hit_stored or len(page) < _PAGE_LIMIT:
            break
        before = page[-1]["id"]
    collected.reverse()
    return collected


async def _sync_manifold_comments(workdir: Path, entry: MarketEntry, *, paced_get: PacedGet) -> None:
    comments: list[dict[str, object]] = []
    page_index = 0
    while True:
        url = f"{MANIFOLD_API}/comments?contractId={entry.market_id}&limit={_PAGE_LIMIT}&page={page_index}"
        page = json.loads(await paced_get(url))
        if not isinstance(page, list):
            raise MirrorDataError(f"comments endpoint returned non-list for {entry.market_id!r}")
        comments.extend(page)
        if len(page) < _PAGE_LIMIT:
            break
        page_index += 1
    comments.sort(key=lambda comment: (comment["createdTime"], comment["id"]))
    comments_jsonl_path(workdir, entry.platform, entry.market_id).write_text(
        "".join(_raw_jsonl_line(comment) for comment in comments), encoding="utf-8"
    )
