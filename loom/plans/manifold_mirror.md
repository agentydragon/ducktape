# Manifold mirror: roster-synced markets inside the evidence scraper

Status: **implemented** (2026-06-10; M1 + loaders). Companion to the
"Manifold mirroring via the live API" section of <market_harvest.md>.
Deviations from the original plan are listed at the bottom.

## Why

No Manifold dump newer than 2024-07-06 exists (verified 2026-06-10 against
the docs page and the Firebase `trade-dumps/` listing). Recent resolved
markets — the only market tasks admissible for modern contestants — and
forward capture of still-open markets both require pulling the live API
ourselves. Git is the right canonical store: the scraper's dated commits make
the mirror an as-of record (checking out an old commit _is_ "what the market
said then"), which is the M2 forward-capture story falling out for free.

## What was built

- **Scraper**: `finance/scraper/market_mirror.py`, a market pass in the
  evidence scraper (`finance/scraper/fetch.py`, deployed entrypoint
  `scrape.py`). Per-market layout under `markets/<platform>/<market_id>/`:
  - `market.json` — the platform's response body verbatim, overwritten each
    sync (git history = the state timeline);
  - `bets.jsonl` — ascending `createdTime`, append-only; incremental sync
    pages `/v0/bets` newest-first until the newest stored bet id, then
    appends the gap atomically (Manifold deep entries only);
  - `comments.jsonl` — full deterministic rewrite each sync (comments are
    editable/deletable upstream).
- **Roster**: deployment config, not code — ConfigMap-mounted YAML files
  (`--roster`, schema `finance.evidence.markets.MarketRoster`; format
  documented by `finance/evidence/example_market_roster.yaml`), unioned with
  every market the calibration catalogs reference (`--catalog`).
- **Pacing**: one global limiter (`RequestPacer`, 0.2s min interval ≈ 300
  req/min) well under Manifold's 500 req/min; reuses `http_fetch`'s
  tenacity/User-Agent discipline.
- **Freshness**: markets share the manifest/staleness machinery with the
  classic sources but default to syncing every run (`--market-max-age-hours
0`) while FRED/Yahoo/Zillow keep their 20h window — an hourly CronJob
  refreshes quotes without re-hammering the slow sources. Resolved markets
  converge to no-op syncs (identical payloads → unchanged tree → no commit).
- **Loaders** (`finance/evidence/manifold.py`): typed records for
  market/bets/comments, jsonl → polars frames, and `prob_at(bets, when)` =
  `probAfter` of the last bet at-or-before `when`. Snapshot record models for
  the other platforms in `finance/evidence/{kalshi,polymarket}.py`.

## Consumers

- **augur calibration** reads quotes from the mirror
  (`finance/augur/calibration/evidence_clients.py` over the git-sync'd
  `AUGUR_EVIDENCE_DIR` checkout). The live per-platform clients and the
  valkey read-through cache + warm-cache CronJob were **deleted** — no
  market-API network I/O in the server at all.
- **loom** (future): gym market tasks recompute prob-at-`as_of` from the
  mirror; the G1 bulk harvest joins dump + mirror across the 2024-07 seam;
  the H3 API-era sweep reuses the `finance/scraper` sync primitives.

## Deviations from the original plan

- **Three platforms, not one**: the mirror also snapshots Polymarket/Kalshi
  `market.json` (one GET each) for every catalog-referenced market, replacing
  the valkey cache wholesale (decided 2026-06-10) — the original plan kept
  valkey as the live-read path. Kalshi bonus: Kalshi purges its settled back
  catalog, so the dated git snapshots are the only history that will exist.
- **Layout**: `markets/<platform>/<id>/`, not `manifold/<id>/`.
- **Roster is a ConfigMap-mounted YAML**, not a checked-in Python module —
  `loom/gym/market_seed_tasks.py` never existed; the curated panel joins the
  deployed roster when it materializes.
- **MULTIPLE_CHOICE markets are fine for deep capture** (raw capture is
  shape-agnostic; bets carry `answerId`); whole-market `prob_at` is
  binary-only and MC consumers pre-filter on `answer_id`.

## Remaining (was M3)

- Skip-resolved hardening: a resolved market whose history is fully synced
  still costs ~3 requests/run to confirm nothing changed; a manifest flag
  could skip it outright (cheap, deferred until the roster is big enough to
  care).
- Roster growth from the harvest pipeline (G1/H3).
