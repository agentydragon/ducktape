# augur

Probabilistic simulator of a multi-agent economic system. Given a
**scenario** — a bundle of agents, assets, liabilities, external series, and
policies — augur produces a distribution over trajectories of state by
sampling many rollouts.

This package contains the generic framework: typed entity model, vectorized
engine, real-estate / ownership / private-equity / tax math, exogenous models,
FastAPI scaffolding, and React shell. User-side configuration (specific
properties, holdings, agent identities, fitted models, deployment) is
composed in downstream user repos via the `Config` schema in
<api/config.py>.

See <SPEC.md> for the entity taxonomy + per-rollout evaluation loop.

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

| Directory   | Purpose                                                                                                                                                                                                                                                  |
| ----------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `model/`    | Runtime exogenous-provider configs, sim-facing exogenous model APIs, simple fixture provider, and the active VECM provider.                                                                                                                              |
| `fit/`      | Offline exogenous-model fitting entry points and config templates.                                                                                                                                                                                       |
| `data/`     | `fetch_real_history.py`: keyless live-refresh of recent monthly macro history (FRED/Yahoo) for the `x/pm_reifier` backtests. (The exogenous evidence the model fits against is scraped into the `augur-evidence` repo and read at `AUGUR_EVIDENCE_DIR`.) |
| `api/`      | `Config` schema, wire request/response shapes, `Backend`, HTTP server, catalog/settings/calibration assembly, OpenAPI schema export.                                                                                                                     |
| `sim/`      | Deterministic trajectory evaluation over typed scenarios and sampled external-series bundles.                                                                                                                                                            |
| `frontend/` | React app + Tailwind bundle build, frontend helpers (casing conversion, columnar table marshaling, scenario-set state, backend client).                                                                                                                  |

## Deployment integration

The production server is API-only: `//augur/api:server` reads a `Config`
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

Prediction-market calibration can use a shared Redis/Valkey read-through cache
by setting `AUGUR_MARKET_CACHE_URL` to a `redis://` or `valkey://` URL. The
generic `AUGUR_CACHE_URL` is also accepted for deployments that share one cache
for multiple Augur data classes. Market snapshots are fresh for
`AUGUR_MARKET_CACHE_TTL_SECONDS` seconds (default 12h) and retained for
`AUGUR_MARKET_CACHE_RETENTION_SECONDS` seconds (default 48h) so stale snapshots
can be served if an upstream market API is temporarily unavailable. If no cache
URL is configured, Augur keeps the previous process-local TTL cache behavior.

Property media stays outside the generic frontend bundle. Deployments publish
images through their own static host or CDN, then declare stable
`property_source.property_assets` entries in config. Each entry binds a
property ID to a deployment-owned asset ID and either an explicit public
`image_url` or the shared `property_source.asset_base_url/{asset_id}` URL.

For local public-fixture development, use the combined dev-only wrapper:

```bash
bazelisk run //augur:dev
```

The public fixture config uses a composite exogenous provider: an independent
macro block plus a deterministic `private_equity_risk` fixture issuer. Fitted
macro models are selected in `Config.exogenous_provider` YAML, e.g. `type:
vecm` with a trained blob path or `type: state_space` with a trained artifact
path plus grouped conditioning observations.

## Profiling

Use `//augur/api:profile_metric_fan` for a focused backend profile of one
product API metric-fan request:

```bash
bazelisk run --config=nolint //augur/api:profile_metric_fan
```

The default request runs 50 rollouts over 100 months through
`Backend.product_metric_fan`, using the public fixture config, configured
public-security portfolio lots, inflation-indexed spend, and the simple
exogenous provider. It writes cProfile data to `/tmp/augur_metric_fan.prof`
and prints the top cumulative functions. The target is guarded by
`--max-seconds=60`; retune request size with `--rollout-count`,
`--horizon-months`, `--metric`, and `--percentiles` when profiling a different
shape.

The simulator runs through the JAX dense-array engine. Collect a profile for
the target request shape directly:

```bash
bazelisk run --config=nolint //augur/api:profile_metric_fan -- \
  --profile-output=/tmp/augur_metric_fan_jax.prof
```
