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

## Layout

| Directory   | Purpose                                                                                                                                 |
| ----------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `model/`    | Runtime exogenous-provider configs, sim-facing exogenous model APIs, simple fixture provider, and the active VECM provider.             |
| `fit/`      | Offline exogenous-model fitting entry points and config templates.                                                                      |
| `data/`     | Public exogenous evidence blobs (FRED series, Yahoo SPY adjusted-close, Zillow ZHVI). Acquisition recipes in `SOURCES.md`.              |
| `api/`      | `Config` schema, wire request/response shapes, `Backend`, HTTP server, catalog/bootstrap assembly, OpenAPI schema export.               |
| `sim/`      | Deterministic trajectory evaluation over typed scenarios and sampled external-series bundles.                                           |
| `frontend/` | React app + Tailwind bundle build, frontend helpers (casing conversion, columnar table marshaling, scenario-set state, backend client). |

## Deployment integration

The production server is API-only: `//augur/api:server` reads a `Config`
from `--config`, `$AUGUR_CONFIG_PATH`, or `/etc/augur/config.yaml`, then serves
the `/api/*` routes and `/healthz`. Downstream deployments should serve the
React bundle and private property assets separately, e.g. from an nginx
sidecar.

Property media stays outside the generic frontend bundle. Deployments publish
images through their own static host or CDN, then declare stable
`property_source.property_assets` entries in config. Each entry binds a
property ID to a deployment-owned asset ID and either an explicit public
`image_url` or the shared `property_source.asset_base_url/{asset_id}` URL.

For local public-fixture development, use the combined dev-only wrapper:

```bash
bazelisk run //augur:dev
```

The public fixture config uses the lightweight `simple` exogenous provider. Fitted
macro models are selected in `Config.exogenous_provider` YAML, e.g. `type:
vecm` with a trained blob path.

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

Set `AUGUR_SIM_ENGINE=numba` to profile the Numba simulator backend. Cold Numba
startup is dominated by JIT compilation, so use an explicit cache directory and
warm it with a tiny request before collecting steady-state timings:

```bash
NUMBA_CACHE_DIR=/tmp/augur_numba_cache AUGUR_SIM_ENGINE=numba \
  bazelisk run --config=nolint --remote_executor= //augur/api:profile_metric_fan -- \
  --horizon-months=1 --rollout-count=1 --max-seconds=180

NUMBA_CACHE_DIR=/tmp/augur_numba_cache AUGUR_SIM_ENGINE=numba \
  bazelisk run --config=nolint --remote_executor= //augur/api:profile_metric_fan -- \
  --profile-output=/tmp/augur_metric_fan_numba.prof
```
