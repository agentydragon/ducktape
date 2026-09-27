# augur

Probabilistic simulator of a multi-agent economic system. Given a
**scenario** — a bundle of agents, assets, liabilities, external series, and
policies — augur produces a distribution over trajectories of state by
sampling many rollouts.

This package contains typed financial declarations, stateful execution,
real-estate / ownership / private-equity / tax math, market models,
FastAPI scaffolding, and React shell. User-side configuration (specific
properties, holdings, agent identities, fitted models, deployment) is
composed in downstream user repos via the `Config` schema in
<api/config.py>.

See <SPEC.md> for current financial, policy, failure and reporting contracts.

`sim/session.py` supplies the Python-controlled batch action loop. Stateful
reduced-form TLH portfolios are Python components in `sim/tlh.py`; their private
holdings and basis do not become household policy state.

Runnable composition examples: <x/bounded_spending/README.md> for executable
spending rules, and <x/bond_policies/README.md> for dated-bond policies on shared
discount curves with tax-free household withdrawals. <x/allocation_glide/README.md>
compares executable constant/glide allocation targets with actual funded consumption
and holdings on stipulated paths.
<x/monthly_actions/README.md> submits an explicit monthly sell → pay action list
and reports actual receipts, tax payment and per-path stopping.
<x/joint_spending_allocation/README.md> composes spending flexibility and allocation
on the same synthetic taxable paths, with intended/paid consumption and selected replay.

## Stability tiers

- **Core**, everything outside `study/` and `x/`, composes in the ways this README and
  <SPEC.md> document, and is kept hard to misuse.
- **Studies** (`study/`) are demonstrations, such as reproductions of published results
  (<study/README.md>).
- **Experimental** code (`x/`) is untrusted and may break without notice.

Core must not depend on `study/` or `x/`. Bazel visibility enforces it: every package there is
visible only to `//finance/augur/study:__subpackages__` and `//finance/augur/x:__subpackages__`.
Fitted models live in `x/models/` with their training code until evidence shows one is good
enough for core; core keeps historical replay (`model/historical_windows.py`) and the market-path
and instrument-pricing infrastructure. **Deviation:** the app (`api/`, `product/`,
`calibration/`) still selects its economy model from `x/models/`, through a per-target
visibility exception marked `CLEANUP` in `x/models/BUILD.bazel`.

## Planning boundary

Public, generic Augur work is tracked in this repo: simulator contracts,
policy/runtime/schema shape, tax/accounting behavior, exogenous-provider
interfaces, public app framework, and generic catalog/storage contracts for
properties, locations, and property assets.

Downstream user repos track private composition: specific agent identities,
holdings, property shortlists, media, deployment manifests, and
company-/person-specific modeling assumptions.

### Public/private evidence boundary

Ducktape is the source of truth for shared Augur conventions, generic modeling
interfaces, public reference classes, public data acquisition recipes, and public
forecasting notes. It may define schemas and evidence categories, and it may include
sourced public issuer-specific facts when those facts are useful to a generic
forecasting or modeling discussion.

Downstream private repos hold evidence that identifies a person, account, issuer,
security holding, property, or deployment. Examples include Shareworks/account
snapshots, exact share counts, security numbers, holder status, tender eligibility,
plan documents, transfer restrictions, property shortlists, private config, and
trained artifacts whose contents encode private observations.

When a forecast or model needs both public and private evidence, keep the generic
method, source taxonomy, and public issuer facts here; keep private holder facts
downstream; and have downstream notes point back to this convention instead of
duplicating it. For example, Ducktape may say a private-company equity forecast is
motivated by an OpenAI holding and may cite public OpenAI financing, valuation,
governance, and liquidity sources. It must not include private quantities, security
numbers, account screenshots, Shareworks-only facts, private documents, personal
eligibility terms, or other holder-specific account details.

## Layout

`sim/tax_profile.py` resolves filing status and currency quantum into immutable
tax profiles with variable-length jurisdiction/bracket records and typed income
categories. A world declares those records as they are; it does not reconstruct tax
schedules from padded arrays or reread jurisdiction rules.

| Directory   | Purpose                                                                                                                                 |
| ----------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `model/`    | Sim-facing exogenous model APIs, market paths and instrument pricing, historical replay, and independent per-series level specs.        |
| `fit/`      | Shared evidence loading and scoring for offline exogenous-model fitting.                                                                |
| `x/models/` | Experimental fitted providers with their training code, and the provider-config union the app selects from.                             |
| `api/`      | `Config` schema, wire request/response shapes, `Backend`, HTTP server, catalog/settings/calibration assembly, OpenAPI schema export.    |
| `sim/`      | Deterministic trajectory evaluation over typed scenarios and sampled external-series bundles.                                           |
| `frontend/` | React app + Tailwind bundle build, frontend helpers (casing conversion, columnar table marshaling, scenario-set state, backend client). |

## Deployment integration

The production server is API-only: `//finance/augur/api:server` reads a `Config`
from `--config`, `$AUGUR_CONFIG_PATH`, or `/etc/augur/config.yaml`, then serves
the `/api/*` routes and `/healthz`. `/api/deployment` reports the deployed
API/frontend source commits when the runtime manifest provides image tags via
`AUGUR_API_IMAGE_TAG` and `AUGUR_FRONTEND_IMAGE_TAG` (or explicit
`AUGUR_API_SOURCE_COMMIT` / `AUGUR_FRONTEND_SOURCE_COMMIT`). Downstream
deployments should pass those as ordinary environment values, typically with
Flux ImagePolicy `:tag` markers, so commit visibility does not stamp the image
contents or defeat digest-based release deduping.

Downstream deployments should serve the React bundle and private property
assets separately, e.g. from an nginx sidecar.

Prediction-market calibration reads market quotes from the augur-evidence
checkout (`AUGUR_EVIDENCE_DIR`, the same checkout the macro anchors use) — no
market-API network I/O at request time. The `finance/scraper` cluster pipeline
that mirrors every catalog-referenced market into that repo is parked, so quotes
date from its last run. Workstation runs (dev server, `calibration_report`)
auto-clone the checkout via `ensure_checkout()` with the
`AUGUR_EVIDENCE_GIT_USERNAME`/`AUGUR_EVIDENCE_GIT_PASSWORD` read credentials.
Resolution, missing-data and model-interpretation boundaries: <docs/calibration.md>.

Property media stays outside the generic frontend bundle. Deployments publish
images through their own static host or CDN, then declare stable
`property_source.property_assets` entries in config. Each entry binds a
property ID to a deployment-owned asset ID and either an explicit public
`image_url` or the shared `property_source.asset_base_url/{asset_id}` URL.

For local public-fixture development, use the combined dev-only wrapper:

```bash
bazelisk run //finance/augur:dev
```

The public fixture config uses a composite exogenous provider: an independent
macro block plus a deterministic `private_equity_risk` fixture issuer. Fitted
macro models are selected per preset in `Config.models`, e.g. `type:
structural_macro`, which defaults to the checked-in fit, or `type: state_space`
with a trained artifact path plus grouped conditioning observations. A checked-in
fit either passes `//finance/augur/x/models/calibrated:sanity_test` or is listed in
its `QUARANTINED` and is not to be used as a model.

## Profiling

Use `//finance/augur/api:profile_metric_fan` for a focused backend profile of one
product API metric-fan request:

```bash
bazelisk run --config=nolint //finance/augur/api:profile_metric_fan
```

The default request runs 50 rollouts over 100 months through
`Backend.product_metric_fan`, using the public fixture config, configured
public-security portfolio lots, inflation-indexed spend, and the simple
exogenous provider. It writes cProfile data to `/tmp/augur_metric_fan.prof`
and prints the top cumulative functions. The target is guarded by
`--max-seconds=60`; retune request size with `--rollout-count`,
`--horizon-months`, `--metric`, and `--percentiles` when profiling a different
shape.
