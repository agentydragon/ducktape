# Manifold mirror: roster-synced markets inside the evidence syncer

Status: **plan** (direction approved 2026-06-10: fold into the existing
augur-evidence scraper rather than a new service). Companion to the
"Manifold mirroring via the live API" section of <market_harvest.md>.

## Why

No Manifold dump newer than 2024-07-06 exists (verified 2026-06-10 against
the docs page and the Firebase `trade-dumps/` listing). Recent resolved
markets — the only market tasks admissible for modern contestants — and
forward capture of still-open markets both require pulling the live API
ourselves. Git is the right canonical store: the scraper's dated commits make
the mirror an as-of record (checking out an old commit _is_ "what the market
said then"), which is the M2 forward-capture story falling out for free.

## Shape — a second source kind in the existing scraper

The scraper (`finance/augur/ingest/fetch.py`) clones the augur-evidence repo,
GETs each single-URL `EvidenceSource` (FRED / Yahoo / Zillow) into the
working tree, and commits-if-changed + pushes. Add a **manifold mirror pass**
alongside `write_sources`:

- **Roster**: `finance/evidence/manifold_roster.py` — tuple of market ids
  (+ slug/comment per entry). Seeded from the curated panel
  (`loom/gym/market_seed_tasks.py` market ids); grows with harvest survivors
  and any forward-capture watchlist.
- **Per-market layout** in the evidence repo, under `manifold/<market_id>/`:
  - `market.json` — current `/v0/market/{id}`, overwritten each sync (git
    history = the state timeline);
  - `bets.jsonl` — ascending `createdTime`, append-only. Incremental sync:
    page `/v0/bets?contractId=…` newest-first until reaching the newest
    stored bet id, then append the gap;
  - `comments.jsonl` — same incremental treatment.
- **Pacing**: one global limiter held well under Manifold's 500 req/min; the
  roster is small and a cold market is only a handful of pages. Reuse
  `http_fetch`'s tenacity/User-Agent discipline.
- **Freshness**: reuse the scraper's per-source freshness-skip machinery —
  a resolved market whose bets/comments are fully synced is frozen upstream,
  so it converges to a cheap no-op; open markets accrue dated diffs (forward
  capture).

## Loaders and consumers

- `finance/evidence` gains manifold record models + loaders (market / bets /
  comments → typed polars frames; `prob_at(bets, when)` = `probAfter` of the
  last bet at-or-before `when`). Unify the record models with
  `loom/harvest/manifold_dump.py` — dump records and API records are
  near-identical shapes — so dump-era (≤ 2024-07) and API-era (roster start →
  now) data parse through one model set.
- Consumers: (1) the gym's market tasks become reproducible from the mirror —
  market prob at `as_of` recomputable in tests instead of trusted from
  one-off fetches; (2) M2 forward capture via git history; (3) the future G1
  bulk harvest joins dump + mirror across the 2024-07 seam.
- The augur calibration `PriceClient` + valkey cache stays the **live-read**
  path (server latency); promoting those clients to a shared package happens
  if/when loom needs live reads — the mirror does not depend on it.

## Phasing

- **M1**: roster + mirror module + scraper wiring + hermetic tests (fixture
  JSON, no network); first sync commits the panel's markets.
- **M2**: loaders in `finance/evidence`; gym tests recompute the panel's
  prob-at-`as_of` from the mirror (guards silent drift).
- **M3**: freshness/no-op hardening; roster growth from the harvest pipeline.
