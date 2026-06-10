# G1 market harvest — verified source survey + pipeline design

Status: **investigation + design** (2026-06-09). Every endpoint, count, and
boundary below was verified with live requests or actual downloads on
2026-06-09 (keyless, from a datacenter IP); extrapolations are marked as such.
Consumes: `loom/gym/task.py` (Task schema), `loom/gym/model_cutoffs.py`
(admissibility). Feeds: the market-resolved task family (program plan G1).

## TL;DR

| source        | keyless?                  | backfill depth                              | price-as-of    | comments-as-of | verdict                                                  |
| ------------- | ------------------------- | ------------------------------------------- | -------------- | -------------- | -------------------------------------------------------- |
| Manifold      | yes (API + bulk dump)     | full site to 2024-07-06 (dump); API onward  | ✅ verified    | ✅ verified    | **primary backfill source**; needs heavy quality filters |
| Polymarket    | yes (Gamma + CLOB + data) | markets to 2020; price history only ~2023+  | ✅ verified    | ✅ verified    | **primary for `as_of ≥ 2023`**, dominant for 2024-07+    |
| Kalshi        | yes (trade-api v2)        | **purged** — settled catalog ≈ last ~30–60d | ✅ recent only | none           | forward-capture only (fold into M2)                      |
| Metaculus     | **no** — 403 keyless      | n/a                                         | n/a            | n/a            | deferred; free account token required                    |
| ForecastBench | yes (GitHub, CC BY-SA)    | 2024-07 onward, biweekly 500-question sets  | freeze value   | no             | cross-check + extra tasks; not a dossier source          |

## Per-platform findings

### Manifold Markets

**Endpoints** (all keyless; base `https://api.manifold.markets/v0/`):

- `GET /markets?limit=1000&before=<id>` — all markets, cursor-paged.
- `GET /search-markets?term=&filter=resolved&contractType=BINARY&limit=...` —
  server-side resolved/type filtering (verified; also `sort=liquidity`).
- `GET /market/{id}` — full detail incl. `description` (TipTap JSON),
  `textDescription` (plain), `groupSlugs`, `resolution`, `resolutionTime`.
- `GET /bets?contractId=...&limit=1000&before=<betId>` — full bet history,
  cursor-paged newest-first. Each bet: `createdTime`, `probBefore`,
  `probAfter`, `amount`, `outcome`, `isRedemption`.
- `GET /comments?contractId=...` — dated comments (TipTap `content`,
  `createdTime`).

**Full-site dump** (the big prize): `https://docs.manifold.markets/data` links
three Firebase zips, HEAD-verified live today —
`manifold-{dump-bets-04072024,contracts-20240706,comments-20240706}.json.zip`
(1014MB / 91MB / 133MB). License: **personal/academic/non-commercial only**
(commercial + AI-training-for-commercial requires a license from
`data@manifold.markets`). The contracts dump was downloaded and parsed in
full:

- **130,091 contracts** total (platform inception Dec 2021 → 2024-07-06);
  100,325 `BINARY`, 22,672 `MULTIPLE_CHOICE`, rest polls/stonks/numeric/etc.
- **67,223 resolved binary**: NO 33,238 / YES 26,781 / **CANCEL 5,811 (8.6%)**
  / MKT 1,393 (2.1%).
- Quality slices (resolved YES/NO binary): `uniqueBettorCount ≥ 10` → 37,110;
  **≥ 20 → 15,815**; ≥ 50 → ~4,000; ≥ 100 → ~1,300.
- Resolution years: 2022: 7,942 / 2023: 38,458 / 2024 (H1): 20,810.
- Markets **open** at candidate `as_of` dates (≥ 20 bettors, YES/NO pool):
  2022-10-01: **886**; 2023-01-01: 2,200; 2023-07-01: 3,678; 2024-01-01:
  **4,005**; (2024-06-01: 619 — artificially low, dump-edge censoring).
- Lifetime (created→resolved) p10/p50/p90 = 3.3 / 47 / 347 days — plenty of
  room for mid-life snapshots on the ≥-month-lived majority.

**Price-at-date + comments-before-date reconstruction — verified end to end**
on `a3k1RgxAVYiAU1qeA5Su` ("January 2024: Will Bitcoin hit $50,000?",
resolved NO): 1,092 bets fetched in 2 pages; price as of 2024-01-15 00:00 UTC
= `probAfter` of last prior bet = **0.139** (historically sane); 8 of 10
comments dated before that cutoff. Reconstruction rule: sort bets by
`createdTime`, take last bet ≤ cutoff, read `probAfter`.

**Rate limits**: documented **500 req/min/IP**; no throttling observed
(~25 requests).

**Data quality issues** (why Manifold needs the heaviest filtering):

- 8.6% CANCEL (N/A) and 2.1% MKT (resolved-to-probability) — both dropped.
- Creator-resolved markets: resolution is the creator's judgment call;
  self-referential/personal markets ("Will I …", "Will @user walk 100k
  steps…") are common even at high liquidity (see top-liquidity sample).
- Joke/meta markets, "resolves to my whim" markets, markets about Manifold
  itself.
- Description is editable post-creation and the API serves only the current
  text (`lastUpdatedTime` exists, no edit history) — a post-`as_of` edit is
  undetectable via API. The dump is a 2024-07-06 snapshot, which bounds edit
  recency for older `as_of`.
- Duplicate/near-duplicate questions across creators are endemic.

### Polymarket

**Endpoints** (all keyless):

- Gamma `GET https://gamma-api.polymarket.com/markets?closed=true&...` —
  filters verified: `end_date_{min,max}`, `volume_num_min`,
  `order=volumeNum&ascending=false`. **`limit` silently caps at 100**;
  `offset` rejected somewhere above 10k ("use `/markets/keyset`").
  `GET /markets/keyset` verified — pages from market id 12 (2020) upward.
  Fields: `question`, `description` (resolution criterion + resolution
  source), `outcomes`/`outcomePrices` (realized outcome, e.g. `["1","0"]`),
  `conditionId`, `clobTokenIds`, `events`, `volumeNum`, `endDate`,
  `closedTime`, `umaResolutionStatus`, `negRisk`, `automaticallyResolved`.
- Gamma `GET /events?closed=true...` — event = group of legs;
  `negRisk: true` ⇒ mutually exclusive leg set (verified on event 903193
  "Presidential Election Winner 2024", 17 legs, `commentCount` 142,827).
- CLOB `GET https://clob.polymarket.com/prices-history?market=<clobTokenId>`
  with `startTs/endTs` or `interval=max`, `fidelity=<minutes>` — verified:
  daily series for the Trump-2024 YES token returns 0.615 on 2024-07-01;
  `interval=max&fidelity=1440` → 307 points 2024-01-05→2024-11-06.
- Gamma `GET /comments?parent_entity_type=Event&parent_entity_id=<id>&order=createdAt&ascending=true`
  — verified; dated bodies, paged.
- Data-API `GET https://data-api.polymarket.com/trades?market=<conditionId>`
  and `/holders` — verified keyless; individual dated trades (alternative
  price reconstruction + unique-trader counting).

**Critical boundary — price history starts with the CLOB (~early 2023)**:
verified that high-volume 2022 markets (e.g. 245033 "Will $ETH be above
$2,000 on May 27?", $800k volume) return **zero** price-history points, as do
the Nov-2022 midterms markets; a May-2023 market (Erdoğan, $4.8M) returns a
full history. AMM-era prices exist only on-chain (Polygon subgraph — a rabbit
hole; not pursued). So Polymarket contributes **price-as-of only for
`as_of ≳ 2023-Q2**` even though market metadata goes back to 2020.

**Scale** (closed markets ending in month, `volume ≥ $10k`, counted by
paging): 2022-10: **32**; 2023-06: **47**; 2024-08: **930**; 2025-06:
**4,520**; 2026-04: **6,100+** (count capped). Pre-2024 Polymarket is thin;
2024+ is huge but flooded with sports dailies and (2025+) 5-minute crypto
up/down markets (current max market id ≈ 2.48M, dominated by that flood).
Top-100 by volume in 2024-08: 88 non-sports / 12 sports.

**Rate limits**: none observed across ~350 requests (gamma + clob +
data-api). No documented public read limit.

**Data quality issues**:

- UMA-oracle resolutions: occasional disputes/"clarifications" that amend the
  criterion mid-life (post-`as_of` description edits undetectable via API —
  same caveat as Manifold). `umaResolutionStatus` should equal `resolved`.
- `[Single Market]`-prefixed duplicates of event legs (observed in 2024-08
  sample) — dedup on `conditionId`/event membership.
- Five-minute crypto and daily sports markets are structurally uninteresting
  for the gym (no dossier, pure noise trading) — excluded by lifetime floor.
- `volumeNum` is lifetime volume (post-`as_of` information) — usable for
  harvest-side filtering (selection, not dossier) but must not enter the
  dossier; as-of volume recomputable from data-api trades if wanted.

### Kalshi

**Endpoints** (keyless reads; base
`https://api.elections.kalshi.com/trade-api/v2/`):

- `GET /markets?status=settled&limit=1000` — cursor-paged; fields incl.
  `result` (`yes`/`no`), `rules_primary` (verbatim criterion), `title` +
  `yes_sub_title`, `open_time`/`close_time`, `volume_fp`,
  `liquidity_dollars`, `event_ticker`.
- `GET /series?category=Economics` — 576 series (KXUE, KXCPIYOY,
  KXFEDDECISION, …).
- `GET /series/{series}/markets/{ticker}/candlesticks?start_ts=&end_ts=&period_interval=1440`
  — verified: daily candles with `yes_bid`/`yes_ask` OHLC, last-trade
  `price`, `volume_fp`, `open_interest_fp`.

**Critical finding — the settled back catalog is purged.** Verified
exhaustively: only **68** settled markets closing before 2025-07-01 are
served (8 in 2024-05, 60 in 2024-10); known 2022–2023 tickers
(`FED-23DEC-T5.50`, `CPI-22DEC-A0.1`) return `not_found`; dropping the status
filter reveals only 147 pre-2024-09 markets (139 zombie `closed`, 8
`finalized`). Mass retention starts only weeks back (12k+ settled in 2026-05
alone). **Historical backfill via the public API is impossible; Kalshi is
forward-capture only** (the M2 snapshot/resolution store).

Other Kalshi gotchas, verified: the latest-1000 settled page was 100%
`KXMVE*` sports-parlay autogen (series/category filtering is mandatory);
series tickers were mass-renamed to `KX*` (old markets answer to the new
series via `event → series_ticker`, e.g. `RHGOLD-24-Q1` → `KXRHGOLD`);
combining `min_close_ts` + `max_close_ts` returns 0 rows (server-side bug —
window client-side instead); `kalshi.com` web pages (not the API) 429
datacenter IPs instantly. ~100 API requests, zero throttling.

### Metaculus

`GET /api/posts/?statuses=resolved...` and legacy `GET /api2/questions/...`
both return **403: "The API is only available to authenticated users"** —
including single-question reads. A token is free (account registration), so
this is a soft gate, but it fails this investigation's no-keys constraint and
needs a user decision before use. Worth revisiting: Metaculus has thousands
of resolved questions with unusually clean resolution criteria, dated
community-prediction history (the natural crowd baseline), and long horizons
(good for the older model windows). **Deferred, not rejected.**

### Secondary sources (briefly)

- **ForecastBench** (`forecastingresearch/forecastbench-datasets`, GitHub,
  CC BY-SA 4.0) — verified downloadable. Biweekly 500-question sets since
  2024-07 with `freeze_datetime` (= `as_of`), `freeze_datetime_value` (crowd
  value at freeze — pre-packaged baseline), source mix per set: manifold 74 /
  metaculus 73 / polymarket 73 / infer 30 / dataset-derived 250
  (ACLED/DBnomics/FRED/wikipedia/yfinance). Resolution files live in the same
  repo. No dossiers (background text only). Use: cross-validation of our
  harvest pipeline, a second opinion on resolution correctness, and a
  ready-made human/LLM leaderboard to sanity-check our baseline numbers.
  Note: raw GitHub fetches fine; the GitHub REST API rate-limits anonymous
  datacenter IPs immediately — use `raw.githubusercontent.com`.
- **Good Judgment / GJOpen** — no public API or export. The IARPA ACE / GJP
  archive (Harvard Dataverse) covers 2011–2015: every `as_of` predates every
  registry model's knowledge cutoff, so it is **inadmissible for all LLM
  contestants** (their weights contain the outcomes); usable only for the
  classical pipeline, which already has unlimited series tasks. Skip.
- **Autocast** (Metaculus+GJOpen-derived academic dataset) — stale (2022),
  redistributes sources with unclear licensing. Skip in favor of first-party
  harvests.

## Task mapping

| market shape                                                | gym question                                                               | outcome                                  |
| ----------------------------------------------------------- | -------------------------------------------------------------------------- | ---------------------------------------- |
| Manifold BINARY resolved YES/NO                             | `BinaryQuestion`                                                           | `BinaryOutcome(value=resolution=="YES")` |
| Manifold MULTIPLE_CHOICE, `shouldAnswersSumToOne=true`      | `CategoricalQuestion` (answers as categories, `ordered=False`)             | winning answer                           |
| Manifold MC sum-to-one=false ("CHOOSE_MULTIPLE")            | per-answer `BinaryQuestion`s (independent legs)                            | per-answer YES/NO                        |
| Polymarket standalone binary (`outcomes=["Yes","No"]`)      | `BinaryQuestion`                                                           | `outcomePrices` → `["1","0"]`=YES        |
| Polymarket `negRisk` event (mutually exclusive legs)        | one `CategoricalQuestion` per event (legs as categories), **not** the legs | the leg priced `"1"`                     |
| Kalshi binary market (forward-captured)                     | `BinaryQuestion`                                                           | `result=="yes"`                          |
| Kalshi threshold ladder within one event (e.g. CPI buckets) | `CategoricalQuestion` with `ordered=True` (RPS-scorable)                   | settled bucket                           |

Excluded outright: Manifold CANCEL (8.6%) and MKT (2.1%) resolutions;
Polymarket markets with `umaResolutionStatus != resolved`; anything
unresolved. Scalar tasks are **not** minted from markets (markets are
indicator-valued; the series family already covers scalars).

**Question text** = market title + the platform's verbatim resolution
criterion (Manifold `textDescription`, Polymarket `description`, Kalshi
`rules_primary`), prefixed with the dating header convention from
`series_tasks.py` ("As of {as_of} …"). `outcome_source` = platform + market
id + resolution timestamp, so outcomes are re-verifiable against the API.

## `as_of` selection and admissibility

- **Calendar-grid snapshots, not per-market offsets**: `as_of` ∈ the same
  quarterly (later monthly) grid the series tasks use. For each market, mint
  a task at every grid date `t` with `createdTime < t < min(closeTime,
resolutionTime)`. This (a) aligns market tasks with the existing
  cluster-bootstrap-by-`as_of` reporting, (b) makes cross-market bundles
  natural (one dossier date), (c) interacts cleanly with model windows: a
  grid date ≥ a model's cutoff makes every task minted at it admissible.
- **Cap per market**: ≤ 2–3 snapshots per market (e.g. earliest grid hit and
  the one nearest mid-life), else long-lived markets dominate. Tasks from one
  market are near-duplicates; record `market_id` so analysis can cluster by
  market as well as by `as_of` (extends the program plan's clustering norm).
- **Dossier cutoff semantics**: `as_of` date `D` means dossier items have
  timestamp `< D 00:00 UTC` and price-as-of = last trade strictly before
  midnight. Consistent with `Task.as_of` ("data dated on or before") since
  grid dates are day-resolution.
- **Snapshot sanity filters** (all computed from pre-`as_of` data, so no
  outcome leakage): ≥ N trades before `t` (price must exist and be live),
  price-as-of within `[0.03, 0.97]` (skip foregone conclusions), market open
  ≥ 7 days before `t`. One unavoidable selection-on-the-future: requiring
  `t < resolutionTime` (not just `closeTime`) drops snapshots that postdate
  an early resolution — that biases _task selection_, not contestant inputs;
  document, don't fight it.
- **Admissibility**: unchanged — `assert_admissible(model, as_of)`. Market
  tasks add what the series family lacks: non-financial diversity
  (geopolitics, science, tech, culture) inside each model's window.

## Dossier composition + leakage rules

Per task, materialized via the existing `dossier.py` →
`materialize_dossier()` path (files mounted at `/data` in the network-less
Inspect sandbox):

| file                | content                                                           | leakage guard                                                |
| ------------------- | ----------------------------------------------------------------- | ------------------------------------------------------------ |
| `question.txt`      | title + verbatim resolution criterion + close date                | creation-time text; see edit caveat below                    |
| `price_history.csv` | `date,prob` daily, from market open through `as_of` (exclusive)   | reconstructed from bets/candles dated `< as_of`              |
| `market_price.txt`  | price-as-of (the crowd baseline, contestant-visible by default)   | last trade `< as_of`                                         |
| `comments.md`       | comment thread, authors anonymized, `createdTime < as_of` only    | per-item timestamp filter (verified feasible both platforms) |
| `meta.txt`          | platform, market age at `as_of`, trade/bettor count up to `as_of` | counts recomputed from dated trades, never lifetime fields   |

**Hard rules**:

1. Every dossier item derives from a raw object carrying its own timestamp;
   the assembler asserts `timestamp < as_of` and refuses undated material.
2. Resolution-bearing fields (`resolution`, `resolutionTime`,
   `outcomePrices`, `result`, `closedTime`, lifetime `volume`) never enter
   the dossier.
3. A CI test greps assembled dossiers for any ISO date / epoch-ms newer than
   `as_of` (cheap, catches whole-object slips).
4. **Description-edit caveat** (accepted, documented): Manifold and
   Polymarket serve only current description text; edits postdating `as_of`
   are undetectable via API. Mitigations: for Manifold `as_of ≤ 2024-07`,
   prefer the dump's 2024-07-06 snapshot text; flag tasks where
   `lastUpdatedTime > as_of`; Polymarket UMA clarifications are rare and
   mostly tighten wording. Residual risk is wording-level, not
   outcome-level.
5. **Linked articles/datasets: deferred to v2.** Only URLs appearing in
   pre-`as_of` text would qualify, and content must come from a Wayback
   snapshot ≤ `as_of` — a correctness minefield with modest dossier value.
   Price history + comments already carry the crowd's reasoning.

Price-as-of is dual-use by design: a contestant-visible feature (default
dossier includes it — the contest is "beat the crowd given the crowd") and
the baseline every contestant is scored against via paired per-task deltas
(`compare_runs.py`). A no-price dossier variant is a cheap ablation flag.

## Quality / dedup filters

Ordered cheap → expensive; each stage records its rejection reason so filter
tuning is data, not vibes:

1. **Mechanical** (per platform):
   - Manifold: resolution ∈ {YES, NO} (or a clean categorical winner);
     `uniqueBettorCount ≥ 20` (15,815 markets pass in the dump); volume
     floor (mana volume ≥ ~1,000); lifetime ≥ 14 days.
   - Polymarket: `volume ≥ $10k`; lifetime ≥ 7 days (kills 5-minute crypto
     and daily sports wholesale); `umaResolutionStatus == resolved`; drop
     `[Single Market]` duplicates of event legs (keep the event-level
     categorical instead).
   - Kalshi (forward): exclude `KXMVE*` parlays and sub-daily series;
     `volume_fp` floor; join `event → series → category` and keep a curated
     category allowlist (Economics, Politics, Science, Companies, …).
2. **Self-resolving / personal / joke (Manifold-specific, the heavy one)**:
   exclude when creator == resolver **and** the question is about the
   creator (heuristics: first/second-person regex (`will i\b`, `my `,
   `@creatorUsername` in title), personal `groupSlugs` (`personal-goals`,
   `fun`, `selfresolving`, `manifold-...` meta groups), creator holds > X%
   of volume). Calibrate on a hand-labeled few-hundred sample; expect to
   lose ~30–50% of the ≥-20-bettor pool.
3. **Resolution-criterion clarity**: require explicit criterion text
   (non-empty description with a resolvable condition). An LLM-graded pass
   (cheap model, rubric: "could two reasonable people disagree on what
   resolves YES?") batched over candidates; threshold tuned on the labeled
   sample. Run once at harvest, cache verdicts with the raw data.
4. **Cross-platform / intra-platform dedup**: the same world-event trades
   everywhere ("Trump wins 2024" on all three platforms; dozens of Manifold
   clones). Embed `title + criterion`, cluster within (resolution quarter ×
   cosine ≥ threshold), keep one representative per cluster per `as_of`
   (prefer the most liquid platform listing); store `cluster_id` so scoring
   can alternatively down-weight instead of drop. This is the gym-side
   sibling of M1's near-duplicate market clustering.
5. **Topic balance** (optional, last): cap any single theme (e.g. AI
   timelines on Manifold, US politics on Polymarket) to a share of each
   `as_of` cohort, so aggregate loss isn't one topic's difficulty.

## Estimated task yields per model window

Bases: verified counts above; "→ tasks" applies the snapshot grid (≤ 2–3 per
market), then assumes ~50% survive filters 2–4 (the one genuinely soft
number; calibrate early on the labeled sample).

- **`as_of ≥ 2022-10-01` (llama-2-class, ~15 quarterly anchors to mid-2026)**
  — Manifold dump is the workhorse: open-at-anchor counts of 886 (2022-10) →
  ~4,000 (2024-01) quality markets per anchor; with per-market caps that is
  roughly 15–25k snapshot-tasks pre-filter → **~8–12k tasks** post-filter
  through 2024-07, plus the API era beyond. Polymarket adds only ~0.5–1k
  (thin era, price history starts ~2023-Q2). Effective-n caveat from the
  program plan applies: thousands of tasks ≈ tens of independent clusters,
  but topic diversity (politics/geopolitics/science/tech) is exactly what
  the series family lacks.
- **`as_of ≥ 2024-07-01` (glm-4.5, ~8 quarterly / 23 monthly anchors)** —
  Polymarket dominates: 930 (2024-08) → 4,520 (2025-06) closed ≥-$10k
  markets/month, of which an estimated 10–25% survive the sports/crypto/
  lifetime filters → order **3–8k markets** over the window → **~5–10k
  tasks**. Manifold API era adds an estimated few hundred quality
  markets/month (post-2024 activity declined; needs one counting sweep to
  pin down). Kalshi contributes only from M2's start date forward.
- Either window upgrades the gym by ~2 orders of magnitude over today's ~100
  admissible tasks per model, and adds the first non-financial cohorts.

## Build plan

### Modules (`loom/harvest/`, new package; gym stays network-free)

| module             | responsibility                                                                                        |
| ------------------ | ----------------------------------------------------------------------------------------------------- |
| `manifold_dump.py` | fetch/verify the three dump zips, stream-parse, emit normalized records                               |
| `manifold_api.py`  | post-2024-07 sweep (`search-markets` paging) + per-market `bets`/`comments` backfill for survivors    |
| `polymarket.py`    | gamma keyset sweep (markets + events), CLOB `prices-history` per `clobTokenId`, comments per event    |
| `kalshi.py`        | forward settled sweep (cursor, client-side close windows), candlesticks, `event→series` category join |
| `record.py`        | normalized Pydantic records: `MarketRecord`, `PricePoint`, `CommentRecord` (platform-tagged)          |
| `quality.py`       | mechanical + self-resolving + clarity filters; emits (record, verdict, reason)                        |
| `dedup.py`         | embedding clustering → `cluster_id`                                                                   |
| `snapshots.py`     | grid `as_of` selection, price-as-of, dossier assembly + the leakage assertions                        |
| `store.py`         | S3 IO under the `harvest/` prefix (below)                                                             |

Task minting lives gym-side as `loom/gym/market_tasks.py` (mirrors
`series_tasks.py`): reads the normalized store, emits `Task` objects +
dossier files. One `py_library` per file per Gazelle convention; tests are
hermetic against fixture excerpts of the real payloads captured during this
investigation (no network in tests).

### Storage (cluster S3, bucket `loom-gym`; `runs/` is taken — use `harvest/`)

```
s3://loom-gym/harvest/
  raw/manifold-20240706/*.json.zip                                # ✅ mirrored verbatim (~1.2GB) + README + checksums.sha256
  raw/manifold/api/<sweep-date>/markets.jsonl.gz                  # + per-market bets/comments for filter survivors only
  raw/polymarket/<sweep-date>/{markets,events}.jsonl.gz
  raw/polymarket/<sweep-date>/prices/<clobTokenId>.json
  raw/polymarket/<sweep-date>/comments/<eventId>.jsonl.gz
  raw/kalshi/<sweep-date>/...                                     # forward-capture; shared layout with M2 snapshots
  norm/v1/{markets,prices,comments}.parquet                       # the queryable layer (polars, matches WorldSet stack)
  tasks/<mint-version>/tasks.jsonl + dossiers/<task_id>/*         # exactly what the gym consumes
```

Raw is immutable and append-only (sweep-dated); `norm/` and `tasks/` are
versioned rebuilds from raw — re-mintable when filters change without
re-scraping. The Manifold dump mirror is done and declarative: the
`manifold-dump-mirror` Job in `cluster/k8s/loom-harvest/` re-verifies it on
every apply (idempotent skip-if-present, sha256-pinned).

### Manifold mirroring via the live API (recent markets; design 2026-06-10)

Implementation plan: <manifold_mirror.md> (fold into the existing
augur-evidence scraper as a second source kind — decided 2026-06-10).

**No newer dump exists** (verified 2026-06-10: the docs page says "Data dumps
last updated: July 6, 2024", and the Firebase `trade-dumps/` listing holds
only the 2023-04 and 2024-07 generations). Everything after 2024-07 — the
window where modern contestants like glm-4.5 are admissible — therefore comes
from the live API, which we mirror ourselves:

- **Shape**: a configured roster of markets to mirror + a syncer running well
  under the ~500 req/min limit, pulling per market: `/v0/market/{id}` (full
  detail), `/v0/bets?contractId=` (append-only → incremental fetch after the
  newest stored bet), `/v0/comments?contractId=`. The curated panel's market
  ids (`loom/gym/market_seed_tasks.py`) are the initial roster entries, so
  panel data becomes reproducible from the mirror rather than ad-hoc fetches.
- **Reuse augur's market-client infra**: `finance/augur/calibration/` already
  has per-platform clients (`manifold.py`, `polymarket.py`, `kalshi.py`
  behind the `PriceClient` protocol, with `transient_retry.py`) and a
  read-through valkey cache (`redis_cache.py`). Per the shared-code floor,
  promote the platform clients to a shared package (e.g. `finance/markets/`)
  when loom starts consuming them — same move as `finance/evidence`.
- **Storage split** (canonical vs bulk vs cache):
  - **augur-evidence git = the canonical mirror.** Same pattern as the
    FRED/Yahoo scrapes: the scraper CronJob (which already has per-source
    freshness skip) commits dated raw files; consumers `ensure_checkout()`.
    Bets as append-only JSONL diff cleanly. The decisive bonus: **git history
    timestamps every sync**, so the mirror doubles as the M2 forward-capture
    record — checking out an old commit _is_ the as-of view of what the
    market said then, for still-unresolved markets.
  - **Bucket (`s3://loom-gym/harvest/raw/`)** = bulk one-shot archives (the
    2024 dump lives there) and the overflow home if per-market mirrors
    outgrow git comfort (hundreds of MB).
  - **Valkey** = the volatile read-through cache for live interactive use
    (augur calibration server). TTL'd and best-effort — never the canonical
    store.

### Phasing

1. **H1 — Manifold dump → tasks** (pure offline; biggest llama-window yield):
   mirror dumps, normalize, filters 1–2, grid snapshots, first market-task
   eval run. Gate: a glm-4.5 + llama-class run on a few hundred market tasks
   with price-as-of baselines, CIs clustered by `as_of` and `market_id`.
2. **H2 — Polymarket sweep** (glm-4.5-window yield): keyset sweep + price
   history + comments for filter survivors; negRisk categorical minting.
3. **H3 — Manifold API era**: post-dump sweep; per-market bets fetch is the
   slow part (~1–2 requests/market within 500/min) — restrict to survivors.
4. **H4 — Kalshi forward capture**: fold into the M2 snapshot CronJob (same
   store); Kalshi tasks accrue from M2 start date onward.
5. **H5 — clarity-filter calibration + dedup**: hand-label sample, tune
   filters 2–4, publish per-filter rejection stats in the harvest report.
6. **Deferred**: Metaculus (needs user OK for a free account token —
   community-prediction history is the best crowd baseline of any source);
   ForecastBench cross-check (assert our pipeline reproduces their
   freeze values for shared markets); Wayback-pinned linked articles;
   AMM-era Polymarket prices via Polygon subgraph.

### Licensing notes

Manifold dumps + API: personal/academic/non-commercial use OK; commercial AI
training requires a license — fine for this project, but the constraint
travels with the data (note it in `harvest/raw/manifold/README`). Polymarket
and Kalshi public APIs: standard ToS, read-only public data. ForecastBench:
CC BY-SA 4.0 (attribution + share-alike if redistributed).

## Verification log (example ids for re-checks)

- Manifold: market `a3k1RgxAVYiAU1qeA5Su` (BTC $50k Jan-2024; 1,092 bets, 10
  comments, price-as-of-2024-01-15 = 0.139); search-markets resolved-binary
  top-liquidity sample incl. `3xXvCL8aKO0n4ms5KEZq`; MC example
  `ikSUiiNS8MwAI75RwEJf` (2024 election, `shouldAnswersSumToOne=true`).
- Polymarket: market `253591` / condition
  `0xdd22472e552920b8438158ea7238bfadfa4f736aa4cee91a6b86c39ead110917` /
  event `903193` (Trump 2024; price history + comments + trades verified);
  AMM-era no-history example `245033`.
- Kalshi: candlesticks on `KXUE-CAN26MAY-7.4` (series `KXUE`); purge probes
  via `RHGOLD-24-Q1-1.6` (event `RHGOLD-24-Q1` → series `KXRHGOLD`) and
  `not_found` on `FED-23DEC-T5.50`.
- Metaculus: 403 on `/api/posts/?statuses=resolved` and
  `/api2/questions/8632/`.
- ForecastBench: `datasets/question_sets/2026-06-07-llm.json` (500
  questions; source mix verified).
