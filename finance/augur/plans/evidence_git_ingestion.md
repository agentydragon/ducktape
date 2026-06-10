# Plan: git-scraping evidence ingestion

Augur's public exogenous evidence (FRED / Yahoo / Zillow) is scraped daily into a Forgejo
repo and read by the app from a git-sync sidecar's checkout, replacing the formerly-vendored
`finance/augur/data/` blobs. **Implemented on branch `claude/vigilant-keller-cy0633`**: the
blobs are deleted and the loader reads only from `AUGUR_EVIDENCE_DIR`, so what remains is the
one-time in-cluster cutover below.

## Storage model: git is the store

Repo `augur-evidence` (a private Forgejo repo) holds the raw upstream bytes — one file
per series, named by its `output_filename`. Git gives us, for free:

- **History**: every refresh that changed bytes is a commit (FRED revises history and you
  cannot re-fetch a past vintage — the commit log preserves them).
- **Change-detection**: the scraper `git add -A`s; an unchanged upstream stages nothing,
  so Zillow's monthly republish doesn't accrue an empty daily commit.
- **Atomic "latest"**: HEAD is the current set; a reader either sees the old worktree or
  the new one, never a half-written file.

No object store, no per-series pointer protocol, no boto3.

## Shape (where it lives)

- **ducktape** `finance/scraper/` (moved out of `finance/augur/ingest/` 2026-06; the
  scraper is shared infra): the `augur-evidence` image — `evidence` (the git scraper
  CronJob entrypoint, `scrape.py`, which also mirrors prediction markets — see
  `market_mirror.py`) — plus the HTTPS fetcher (`http_fetch`). The static source spec
  stays in `finance/evidence/sources.py`. `tf/gitops/augur-evidence/`: the Forgejo repo +
  write creds (scraper) + read creds (git-sync), mirroring `tf/gitops/budget-ledger/`.
- **gaffer** `k8s/augur/`: the daily scrape CronJob (`evidence-ingest-cronjob.yaml`) and a
  `git-sync` sidecar on the augur Deployment, plus the `augur-evidence` image automation in
  `k8s/flux-image-automation/`.
- **App reads from the synced dir** (`fit/evidence_data`): every evidence read is
  `Path(AUGUR_EVIDENCE_DIR) / output_filename` (the env var is required — a read with it unset,
  or a missing file under it, raises). The git-sync sidecar keeps that directory pointed at the
  repo's current worktree (`GITSYNC_LINK=evidence`), so the loader sees freshly-pulled data
  without a pod restart. Tests (and offline `fit:train` / `fit:metrics_report` runs) point the
  same env var at a generated synthetic set (`fit/synthetic_evidence.py`) or a repo checkout.
  A series' identity is its `EvidenceSource` (`kind/series_id`) — used by the scraper, the
  loaders, and provenance labels alike — never a file path.

## Creds delivery

The Forgejo repo + two service users (writer owner, read-only collaborator) are provisioned
by `tf/gitops/augur-evidence/`. Their k8s Secrets are created in the ducktape-owned `budget`
namespace and reflected (emberstack) into `augur`, exactly as `budget-ledger` delivers its
git creds — so the gaffer-reconciled `augur` namespace gets them without a cross-repo Flux
dependency. The CronJob reads the write creds; the git-sync sidecar reads the read creds.

## Freshness

git-sync re-pulls on `GITSYNC_PERIOD` (1m), atomically swapping the worktree symlink, so each
evidence read reflects the latest pushed commit. `load_absolute_monthly_levels` (via
`macro_anchors.resolve_anchors`) is read per calibration request, so each request reflects the
current checkout. A missing file under `AUGUR_EVIDENCE_DIR` raises, surfacing an un-synced dir
rather than serving stale data.

## Remaining: cutover

The repo must hold data before augur is switched to `AUGUR_EVIDENCE_DIR` reads (a read against
an un-synced dir raises). After merge + the image push + Flux image automation:

1. Run the scrape once: `kubectl create job --from=cronjob/augur-evidence-ingest -n augur`.
2. Confirm the repo's `main` carries every `output_filename`.
3. Roll the augur Deployment (it now carries the git-sync sidecar + `AUGUR_EVIDENCE_DIR`).

## Deferred: per-source fetch cadence

The CronJob refetches every source daily, but most are monthly-or-slower publications: FRED
CPI / Case-Shiller / FHFA / rent-CPI and Zillow ZHVI / ZORI move monthly (FHFA quarterly), and
`MORTGAGE30US` weekly; only the Yahoo SPY / BTC / ETH daily closes and FRED `SP500` genuinely
change every day. Git already no-ops an unchanged refetch (no commit), so this is purely about
not hammering the upstreams — consider splitting the schedule (a daily job for the price
series, a weekly/monthly job for the slow ones) or gating each source on its expected cadence
in the scraper.

## Deferred: Stage 2 — in-cluster re-fit

Re-running `fit:train` against fresh evidence to refresh `latest_observations` + the trained
artifact (the part that actually moves the simulator fans) is out of scope here: JAX/NumPyro-heavy
and gated by `sample_sanity`. When done it should run the gate in-job and publish to a PR for
review. Prod anchors live in gaffer (`state_space_macro_artifact.json` + `config.yaml`
conditioning observations), so the publish target crosses the public→private boundary.
