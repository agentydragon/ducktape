# Server-side result persistence + slicing

## Why

The current `/api/scenario_sets/run` returns the full simulation product
synchronously: ~25 MB per scenario at 128 × 360 with the gated default
`ReportSpec`, ~540 MB if all event-stream gates are flipped on. At the
gaffer-private 15-scenario workload that scales to ~8 GB at response-build
time, which OOM-killed the 4 GiB-limit augur container even after today's
trace-storage refactors (which reduced _in-memory_ trace from ~3 GB Pydantic
to ~5 MB polars per scenario, but didn't touch the response wire).

The four `include_*` gates landed in `96537e7d` knock the _default_ response
down to chart-essential surfaces. That fixes the OOM but leaves three
remaining frictions:

1. **Re-running the same scenario set with one knob changed re-simulates
   from scratch.** Inputs are already content-addressed by `scenario_input_id`
   - `path_set_id`; nothing is cached.
2. **Debug streams (funding decisions, obligations, settlements, failures,
   effects, policy decisions, etc.) are gated off entirely**, so debug UIs
   can't see them at all. Fixing this with bigger default payloads brings
   the OOM back.
3. **The frontend always gets `monthly_columns` for every scenario**, even
   if the user is on the scenario-list overview that only needs summary
   stats. That's tens of MB shipped per re-render.

## Shape

Stateless-snowglobe-style cache, keyed by content.

```
RunCacheKey = (
    scenario_set_id,
    scenario_input_id,        # already content-derived (scenario_input_id())
    market_request_hash,      # rollout_count + horizon + seed + provider
    exogenous_path_set_id,    # for explicit sampled-bundle paths
    code_version_hash,        # so cache invalidates on engine refactors
)
```

The cache value is the in-memory `SimulationRun` plus derived `ProjectionRun`
frames, stored either:

- **In-process LRU** (simplest; sized to the container memory budget),
  invalidated on pod restart. Sufficient for "user changes one knob, hits
  run again 10 s later."
- **On-disk** under `/var/lib/augur/runs/<key>.parquet` per polars frame
  (already an Arrow-native format), and Pydantic-tuple fields serialized
  alongside. Survives pod restart, good for shared workspaces.
- **Object storage** (S3-compatible) for cross-pod sharing if/when augur
  scales horizontally. Off the critical path for now.

LRU is the first cut.

## API

Two-phase: `run` returns a handle; subsequent `GET`s fetch slices.

```
POST /api/scenario_sets/run
  → 201 with { run_id, scenario_set_id, scenario_run_ids: [...] }
    (no scenario_results payload; clients use the slice endpoints below)
  ; idempotent — same input returns same run_id

GET /api/runs/<run_id>/scenarios/<scenario_id>/summary
  → small struct: rollout_statuses, accepted_summary, warnings

GET /api/runs/<run_id>/scenarios/<scenario_id>/monthly_columns
  → existing ColumnarTable; supports ?metrics=cash_usd,net_worth_usd to
    project just specific columns

GET /api/runs/<run_id>/scenarios/<scenario_id>/terminal_columns
GET /api/runs/<run_id>/scenarios/<scenario_id>/metric_fan_columns
GET /api/runs/<run_id>/scenarios/<scenario_id>/rollouts/<i>/series/<metric>
  → single-rollout single-metric series, for the hover-inspection UI

GET /api/runs/<run_id>/scenarios/<scenario_id>/event_stream/<name>
  → funding_decisions | obligations | settlement_results | failure_events
    | effects | policy_decisions | market_observations | tax_lots | ...
    ; paginated (limit + offset) so a stuck rollout's failure_events don't
      blow up the response
```

The slice routes return the in-memory polars columns directly through the
existing `ColumnarTable` shape, so the frontend's existing renderers keep
working with no schema change beyond URL routing.

## Frontend impact

Three changes, in order:

1. The scenario-list page only fetches `…/summary` per scenario.
2. The single-scenario distribution page fetches
   `…/monthly_columns?metrics=…` for the metrics actually shown on the
   active chart panel, plus `…/metric_fan_columns?metrics=…` for fans.
3. Hover-inspect fetches `…/rollouts/<i>/series/<metric>` on demand instead
   of pre-shipping every rollout series.

Each is independent; (1) alone is the largest immediate latency/payload win.

## Out of scope (for now)

- Authentication of run handles. Today augur lives behind oauth2-proxy +
  Authentik OIDC, so the handle is implicitly per-user-session. Cross-user
  sharing would need an explicit ACL on the run cache.
- Cache eviction beyond LRU. Background reclamation, TTL, manual pin/unpin
  are future concerns when shared runs become real workloads.
- Cross-pod sharing. Single replica is fine today.

## Validation

After landing:

```bash
# Peak RSS at the augur app container should stay well below the 4 GiB
# limit even when all event-stream gates are flipped on.
kubectl -n augur top pod -l app.kubernetes.io/name=augur

# Wall time for the scenario-list page should drop from ~30 s for one
# scenario to <500 ms (small summary fetch), and re-running with the
# same inputs should hit the cache.
```
