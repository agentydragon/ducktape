# augur

Probabilistic simulator of a multi-agent economic system. A caller declares a world —
agents, accounts, holdings, liabilities and contracts — samples many market paths from a
model it chooses, and runs one world per path to get a distribution over trajectories.

The package holds typed financial declarations, stateful execution, real-estate / ownership /
private-equity / tax math, market models, FastAPI scaffolding and a React shell. User-side
configuration (properties, holdings, agent identities, fitted models, deployment) is composed
in downstream repos via the `Config` schema in <api/config.py>. Financial, policy, failure
and reporting contracts: <SPEC.md>.

## Using Augur

Augur is building blocks, like PyTorch: the caller owns the rollout loop. **Exogenous models**
(<model/>, <x/models/>: prices, rates, inflation) sample paths first; the **`World`** (<sim/>:
household, counterparties, taxes, settlement) is built on a path and reads it, never feeding
back.

<x/monthly_actions/run.py> is the runnable, CI-tested template. Abridged:

```python
bundle = model.sample(request)  # the model the caller chose
series = compile_series(materialize_sampled_exogenous(bundle), rollout_count=N, horizon_months=H, currency=USD)

worlds = {}
for rollout_id in range(N):
    world = World(MarketPath(series, rollout_id, rollout_count=N), horizon_months=H)
    world.declare_account(account=checking, opening_balance=USD.quanta(200))
    world.declare_pool(...)
    world.hold_lot(..., units=quantity_to_quanta(2, scale=scale), basis=USD.quanta(80))
    world.track(Biller(..., amount_due=USD.quanta(150), schedule=Once(month=0)))
    worlds[rollout_id] = world

session = ActionSession(worlds, HOUSEHOLD)
batch = session.start()
while not isinstance(batch, Finished):
    batch = session.advance(decide(batch))  # the caller's policy
for rollout in batch.rollouts:
    print(rollout.rollout_id, rollout.stop, rollout.summary.cash)
```

1. **Sample paths.** A `Sampler` (<model/exogenous.py>) returns a `SampledExogenousBundle`
   from `sample(ExogenousSamplingRequest(...))`; historical replay
   (<model/historical_windows.py>) from `materialize(...)`. `compile_series`
   (<sim/external_series.py>) turns it into the integer series a world reads. The template
   stipulates two price paths with `ExternalSeriesContext.from_level_blocks` instead.
2. **Build and declare.** One `World` (<sim/world.py>) per path: month-0 state via
   `declare_*`, `hold_lot`, `hold_bond` (keyword arguments, exact integer money), then `track`
   actors — counterparties (`Biller`, `Mortgage`, `TaxAuthority`) and, for `step()`, the
   household. A caller declaring one situation onto many worlds defines its own records.
3. **Act each month.** Track an `EconomicAgent` (<sim/agent.py>), `start()`, then `step()`
   until `finished`, reading e.g. `book()` between steps; or hand the worlds to
   `ActionSession` (<sim/session.py>) and answer each batch of `Decision`s with one
   `DecisionActions` per path.
4. **Read results.** `Finished.rollouts` (<sim/results.py>): each path's `Summary`, optional
   `Trace`, and stop reason.

**Strategies** (spending rule, glide, Guyton–Klinger) are caller code under `study/` or `x/`;
`policy/` holds reusable budget proposers such as `cash_band`. **Models**: historical replay
is core; fitted models live in `x/models/`.

**Money.** `World` counts integer quanta and knows no currency. Callers convert at their edge
through a `Currency` (<sim/money.py>: `USD`, `Currency.quanta`) and the exact helpers in
<sim/fixed_point.py> (`quantity_to_quanta`, `rate_to_ppb`), which raise rather than round.

The Plaid startup source retains imported balances, position values, and cost basis as `Decimal`
while resolving the portfolio. It converts a computed lot quantity to `float` only when creating
`HoldingTaxLotConfig`, whose simulation input is `PositiveFloat`; money continues through the
existing exact currency path.
Sampled paths round only through `sampled_array_to_quanta` / `round_ppb` (applied by
`compile_series`), exact derived amounts only through `round_currency_amount`.

Stateful reduced-form TLH portfolios are Python components in <sim/tlh.py>; their holdings
and basis do not become household policy state. <sim/tax_profile.py> resolves filing status
and currency quantum into immutable tax profiles (variable-length jurisdiction/bracket
records, typed income categories); a world declares them as-is, never rebuilding schedules
from padded arrays.

More compositions: <x/bounded_spending/README.md> (spending rules),
<x/bond_policies/README.md> (dated bonds on shared discount curves, tax-free withdrawals),
<x/allocation_glide/README.md> (constant/glide allocation vs funded consumption),
<x/joint_spending_allocation/README.md> (spending flexibility × allocation on synthetic
taxable paths).

## Stability tiers

- **Core** (outside `study/` and `x/`): composes as this README and <SPEC.md> document; kept
  hard to misuse.
- **Studies** (`study/`): demonstrations, e.g. reproductions of published results
  (<study/README.md>).
- **Experimental** (`x/`): untrusted, may break without notice.

Core must not depend on `study/` or `x/`; Bazel visibility restricts every package there to
`//finance/augur/{study,x}:__subpackages__`. Core keeps historical replay and the market-path
and instrument-pricing infrastructure. **Deviation:** the app (`api/`, `product/`,
`calibration/`) still selects its economy model from `x/models/` via per-target visibility
exceptions marked `CLEANUP` in `x/models/BUILD.bazel`.

## Public/private boundary

This repo tracks and holds generic Augur work and evidence: simulator contracts,
policy/runtime/schema shape, tax/accounting, exogenous-provider interfaces, the public app,
generic catalog/storage contracts, public reference classes, data-acquisition recipes,
forecasting notes, and sourced public issuer facts (e.g. public OpenAI financing, valuation,
governance and liquidity facts motivating a private-equity forecast).

Downstream private repos hold anything identifying a person, account, holding, property or
deployment: agent identities, Shareworks/account snapshots, share counts, security numbers,
holder status, tender eligibility, plan documents, transfer restrictions, property
shortlists, media, private config, deployment manifests, person-specific assumptions, and
trained artifacts encoding private observations. Downstream notes point back here rather
than duplicating this convention.

## Layout

- `model/`: sim-facing exogenous model APIs, market paths, instrument pricing, historical
  replay, per-series level specs
- `fit/`: shared evidence loading and scoring for offline model fitting
- `x/models/`: experimental fitted providers with training code; the provider-config union the
  app selects from
- `api/`: `Config`, wire shapes, `Backend`, HTTP server, catalog/settings/calibration assembly,
  OpenAPI export
- `sim/`: the `World` — declared books, tracked actors, deterministic monthly execution
- `frontend/`: React + Tailwind app, casing/columnar-table helpers, scenario-set state, backend
  client

## Deployment

`//finance/augur/api:server` is API-only: reads a `Config` from `--config`,
`$AUGUR_CONFIG_PATH` or `/etc/augur/config.yaml`; serves `/api/*` and `/healthz`.
The API server and `//finance/augur:dev` accept `--listen-address host:port` (default
`127.0.0.1:8767`) or `--listen-address fd:N` for an inherited listening socket.
`/api/deployment` reports source commits from `AUGUR_{API,FRONTEND}_IMAGE_TAG` (or
`AUGUR_{API,FRONTEND}_SOURCE_COMMIT`); pass them as env values (e.g. Flux ImagePolicy `:tag`
markers), never stamped into the image, which would defeat digest dedup. Deployments serve
the React bundle and property media separately (e.g. nginx sidecar); media is declared as
`property_source.property_assets` entries binding a property ID to an asset ID and either an
`image_url` or `property_source.asset_base_url/{asset_id}`.

Prediction-market calibration reads quotes from the augur-evidence checkout
(`AUGUR_EVIDENCE_DIR`), with no request-time network I/O. The `finance/scraper` mirror is
parked, so quotes date from its last run. Workstation runs auto-clone via `ensure_checkout()`
with `AUGUR_EVIDENCE_GIT_{USERNAME,PASSWORD}`. Boundaries: <docs/calibration.md>.

Local dev on the public fixture: `bazelisk run //finance/augur:dev`. Its config composes an
independent macro block with a deterministic `private_equity_risk` fixture issuer. Fitted
macro models are chosen per preset in `Config.models` (e.g. `structural_macro`, defaulting
to the checked-in fit; `state_space` with an artifact path and grouped conditioning
observations). A checked-in fit must pass `//finance/augur/x/models/calibrated:sanity_test`
or be listed in its `QUARANTINED`, and then is not to be used.

## Profiling

```bash
bazelisk run --config=nolint //finance/augur/api:profile_metric_fan_bin
```

Profiles one `Backend.product_metric_fan` request (default 50 rollouts × 100 months, public
fixture) into `/tmp/augur_metric_fan.prof`, printing top cumulative functions; bounded by
`--max-seconds=60`. Reshape with `--rollout-count`, `--horizon-months`, `--metric`,
`--percentiles`.
