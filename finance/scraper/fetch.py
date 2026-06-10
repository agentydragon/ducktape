"""Git-scrape public evidence upstreams into the augur-evidence repo.

Clones the evidence repo, GETs each public source (FRED / Yahoo / Zillow) with
its per-source User-Agent, writes each into the working tree under its
`output_filename`, runs the prediction-market mirror pass (`market_mirror`) for
the rostered markets, then commits-if-changed + pushes. Git provides history,
change-detection (an unchanged upstream produces the same tree), and an atomic
"latest" (HEAD) — no object store, no pointer protocol.

A small `evidence_meta.json` manifest, committed alongside the data, records each
source's last successful fetch time. It drives two policies the raw data files
can't (git history isn't available — the scraper shallow-clones at depth=1):
  * freshness skip — don't re-GET a source fetched within `max_age` (the daily run
    still refreshes; closely-spaced re-runs stop re-hammering the upstreams);
  * stale-aware exit — a source that fails to fetch is only a Job failure once its
    committed copy is older than `stale_after`, so a transient single-upstream blip
    doesn't turn the whole Job red while the other sources commit fine.

Clone/commit/push go through pygit2 (libgit2); GIT_USERNAME/GIT_PASSWORD drive a
UserPass credential callback, so no credentials are embedded in the remote URL.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import tempfile
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from itertools import chain
from pathlib import Path
from typing import Protocol

import pygit2
from pydantic import BaseModel, Field

from finance.evidence.markets import MarketEntry, load_roster, merged_roster
from finance.evidence.sources import EVIDENCE_SOURCES, EvidenceSource
from finance.scraper.http_fetch import FETCH_ERRORS, HttpGet, http_get as _real_http_get
from finance.scraper.market_mirror import sync_markets

logger = logging.getLogger(__name__)

_AUTHOR = pygit2.Signature("augur evidence scraper", "augur@allegedly.works")

EVIDENCE_META_FILENAME = "evidence_meta.json"

# Skip re-GETting a source fetched within this window: its committed copy is still
# fresh, so refetching would just re-download unchanged data and burn rate limits.
# < 24h so the daily CronJob still refreshes every day.
DEFAULT_MAX_AGE = timedelta(hours=20)

# A source that fails to fetch is tolerated (Job stays green) until its last
# successful fetch is older than this — long enough to ride out transient upstream
# outages, short enough that a genuinely stuck source still surfaces.
DEFAULT_STALE_AFTER = timedelta(days=3)


class EvidenceManifest(BaseModel):
    """Per-source last-successful-fetch times, committed as evidence_meta.json."""

    # provenance_label (e.g. "fred:CPIAUCSL") -> last successful fetch (UTC).
    last_fetched: dict[str, datetime] = Field(default_factory=dict)


def _load_manifest(workdir: Path) -> EvidenceManifest:
    path = workdir / EVIDENCE_META_FILENAME
    return EvidenceManifest.model_validate_json(path.read_text()) if path.exists() else EvidenceManifest()


def _write_manifest(workdir: Path, manifest: EvidenceManifest) -> None:
    (workdir / EVIDENCE_META_FILENAME).write_text(manifest.model_dump_json(indent=2) + "\n")


def _age_since_fetch(manifest: EvidenceManifest, label: str, now: datetime) -> timedelta | None:
    """How long since `label` was last successfully fetched, or None if never recorded."""
    last = manifest.last_fetched.get(label)
    return None if last is None else now - last


class _Provenanced(Protocol):
    @property
    def provenance_label(self) -> str: ...


def _due[ItemT: _Provenanced](
    items: list[ItemT], manifest: EvidenceManifest, now: datetime, max_age: timedelta | None
) -> list[ItemT]:
    """The items whose committed copy is older than `max_age` (all of them when None)."""
    if max_age is None:
        return items
    due = []
    for item in items:
        age = _age_since_fetch(manifest, item.provenance_label, now)
        if age is not None and age < max_age:
            logger.info("skipping %s; fetched %s ago (< %s)", item.provenance_label, age, max_age)
        else:
            due.append(item)
    return due


async def write_sources(workdir: Path, sources: Iterable[EvidenceSource], *, http_get: HttpGet) -> set[EvidenceSource]:
    """GET every source concurrently and write each into `workdir/<output_filename>`.

    Fetches run concurrently (`asyncio.gather`) so the whole set completes in roughly
    the slowest single source's time rather than the sum — the CronJob's 600s
    `activeDeadlineSeconds` can't be blown by serializing a dozen large downloads.
    Per-source upstream failures are logged, not raised (tenacity already rode out
    transient blips, and a bounded per-source retry budget keeps any one stalled
    upstream from eating the whole deadline), so one dead upstream doesn't block
    committing the rest. Returns the set of sources that failed to fetch.
    """
    sources = list(sources)
    bodies = await asyncio.gather(*(http_get(s.upstream_url, s.user_agent) for s in sources), return_exceptions=True)
    failed: set[EvidenceSource] = set()
    for source, body in zip(sources, bodies, strict=True):
        if isinstance(body, BaseException):
            if not isinstance(body, FETCH_ERRORS):
                raise body  # an unexpected error is a bug, not a recoverable upstream outage
            logger.warning("failed to fetch %s", source.provenance_label, exc_info=body)
            failed.add(source)
            continue
        (workdir / source.output_filename).write_bytes(body)
        logger.info("fetched %s -> %s (%d bytes)", source.provenance_label, source.output_filename, len(body))
    return failed


def commit_and_push(
    repo: pygit2.Repository, branch: str, *, now: datetime, callbacks: pygit2.RemoteCallbacks | None = None
) -> bool:
    """Stage everything; commit + push only if the tree changed. Returns True if pushed."""
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    head = repo.head
    head_commit = repo[head.target].peel(pygit2.Commit)
    if tree == head_commit.tree_id:
        logger.info("evidence unchanged; nothing to commit")
        return False
    repo.create_commit(
        head.name, _AUTHOR, _AUTHOR, f"evidence: refresh {now.date().isoformat()}", tree, [head_commit.id]
    )
    repo.remotes["origin"].push([f"refs/heads/{branch}"], callbacks=callbacks)
    logger.info("pushed refreshed evidence to %s", branch)
    return True


async def run_scrape(
    git_url: str,
    branch: str,
    sources: Iterable[EvidenceSource],
    *,
    username: str,
    password: str,
    http_get: HttpGet,
    now: datetime,
    markets: Iterable[MarketEntry] = (),
    depth: int = 1,
    max_age: timedelta | None = None,
    market_max_age: timedelta | None = None,
    stale_after: timedelta | None = None,
) -> int:
    """Clone, refresh the due sources + mirror the due markets, commit-if-changed + push.

    `depth` defaults to a shallow (depth=1) clone — the scraper only needs the tip to write
    the refresh and commit on top of it, and the full history stays server-side in Forgejo.
    Tests against a local filesystem remote must pass `depth=0` (libgit2's local transport
    does not support shallow fetch).

    `max_age` (when set) skips re-GETting any source last fetched within it; `market_max_age`
    is the same policy for the market mirror pass — kept separate (and defaulting to None =
    every run) so an hourly schedule refreshes market quotes while the slow-moving sources
    keep their longer window. `stale_after` (when set) decides the exit code: a source or
    market that failed to fetch only fails the Job once its last successful fetch is older
    than `stale_after` (or was never recorded). With `stale_after=None` any failure fails
    the Job. Returns the process exit code.
    """
    sources = list(sources)
    markets = list(markets)
    # Empty creds (tests against a local filesystem remote) clone without a credential callback.
    callbacks = (
        pygit2.RemoteCallbacks(credentials=pygit2.UserPass(username, password)) if username and password else None
    )
    with tempfile.TemporaryDirectory() as tmp:
        repo_path = Path(tmp) / "repo"
        repo = pygit2.clone_repository(
            git_url, str(repo_path), checkout_branch=branch, callbacks=callbacks, depth=depth
        )
        manifest = _load_manifest(repo_path)

        due_sources = _due(sources, manifest, now, max_age)
        failed_sources = await write_sources(repo_path, due_sources, http_get=http_get)
        due_markets = _due(markets, manifest, now, market_max_age)
        failed_markets = await sync_markets(repo_path, due_markets, http_get=http_get)

        for source in due_sources:
            if source not in failed_sources:
                manifest.last_fetched[source.provenance_label] = now
        for entry in due_markets:
            if entry not in failed_markets:
                manifest.last_fetched[entry.provenance_label] = now
        _write_manifest(repo_path, manifest)
        commit_and_push(repo, branch, now=now, callbacks=callbacks)

        failed_labels = {source.provenance_label for source in failed_sources}
        failed_labels |= {entry.provenance_label for entry in failed_markets}
        stale = {
            label
            for label in failed_labels
            if stale_after is None or (age := _age_since_fetch(manifest, label, now)) is None or age >= stale_after
        }
        for label in stale:
            logger.error("%s is stale: last fetched %s ago", label, _age_since_fetch(manifest, label, now))
    return 1 if stale else 0


def build_parser() -> argparse.ArgumentParser:
    """The generic scraper CLI; deployment entrypoints extend it (see `scrape.py`)."""
    parser = argparse.ArgumentParser(prog="evidence-scraper", description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--git-url", required=True, help="Evidence repo http(s) URL (creds via GIT_USERNAME/GIT_PASSWORD)."
    )
    parser.add_argument("--branch", default="main", help="Branch to clone + push (default: main).")
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=DEFAULT_MAX_AGE.total_seconds() / 3600,
        help="Skip re-GETting a source fetched within this many hours (0 to always refetch).",
    )
    parser.add_argument(
        "--market-max-age-hours",
        type=float,
        default=0,
        help="Skip re-syncing a market synced within this many hours (default 0: sync every run).",
    )
    parser.add_argument(
        "--stale-after-hours",
        type=float,
        default=DEFAULT_STALE_AFTER.total_seconds() / 3600,
        help="Fail the Job only if a source that couldn't be fetched was last fetched longer ago than this "
        "(0 to fail on any fetch miss).",
    )
    parser.add_argument(
        "--roster",
        action="append",
        type=Path,
        default=[],
        help="Market roster YAML to mirror (see finance.evidence.markets.MarketRoster); repeatable.",
    )
    return parser


def _hours_or_none(hours: float) -> timedelta | None:
    return timedelta(hours=hours) if hours > 0 else None


async def run_from_args(args: argparse.Namespace, *, markets: Iterable[MarketEntry]) -> int:
    """Run the scrape from parsed `build_parser()` args; `markets` is the merged roster."""
    return await run_scrape(
        args.git_url,
        args.branch,
        EVIDENCE_SOURCES,
        username=os.environ["GIT_USERNAME"],
        password=os.environ["GIT_PASSWORD"],
        http_get=_real_http_get,
        now=datetime.now(UTC),
        markets=markets,
        max_age=_hours_or_none(args.max_age_hours),
        market_max_age=_hours_or_none(args.market_max_age_hours),
        stale_after=_hours_or_none(args.stale_after_hours),
    )


async def async_main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    markets = merged_roster(chain.from_iterable(load_roster(path) for path in args.roster))
    return await run_from_args(args, markets=markets)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    sys.exit(main())
