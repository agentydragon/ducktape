# Kill magic-prefix wire strings everywhere in augur (staged)

## Goal

Augur encodes the _kind_ of a series/asset in a magic string prefix
(`"crypto:btc"`, `"home_value:san_francisco_ca"`, `"rent:vallejo_ca"`,
`"private_equity:openai"`) and dispatches on it (`.startswith` / `partition(":")`).
The runtime typed boundary (`LevelSeriesKey`, `AssetKey`) already exists, but
prefixes still survive in **config**, **in-memory polars frames**, **on-disk
trained artifacts**, and the **API wire**. The endpoint is zero magic-prefix
strings anywhere — identity carried structurally (typed unions in config/API;
`kind` + `qualifier` columns in frames; typed factor records in artifacts).

This is the columnar/serialized analog of the existing `PrivateEquityBundle`,
which already carries PE state as a bare `issuer_id` column + typed channel
columns rather than magic-prefixed rows.

## Staging contract

The work is **staged** across 4 phases (below). Staging is allowed because each
phase lands in a coherent, green, independently-reviewable state — **and**
because this roadmap commits to the endpoint and is tracked in
`augur/TODO.md` so it cannot be stranded half-done.

**Definition of done (the whole effort):** no magic-prefix string is constructed
or parsed anywhere. `wire_id` / `parse_level_series_key` /
`try_parse_level_series_key` / `parse_asset_key` / `try_parse_asset_key` are
**deleted**. The final phase adds a CI guard (a `rg`-based test) asserting the
source tree contains no `partition(":")` / `startswith("crypto:"|"home_value:"|…)`
series/asset dispatch, so regressions fail the build.

Distinguish "magic-prefix string" (kind encoded in a prefix; must be parsed —
the target) from a **bare identifier** (`issuer_id="openai"`, `symbol="btc"`,
`location_id="san_francisco_ca"` in their own typed column). Bare ids in a typed
column are fine; the PE bundle already uses a bare `issuer_id` column. The goal
is to eliminate prefix-encoding/parsing, not to ban identifier strings.

**No backcompat.** Old configs/artifacts/frames must fail loudly. gaffer-private
may be red between a ducktape phase landing and its gaffer follow-up — accepted.

## Frame shape decision: Option 1 — `kind` + `qualifier` columns

The prefix encodes TWO things (kind discriminator + sub-id), and a single bare
string cannot replace it — `"san_francisco_ca"` would collide between
`home_value` and `rent` (sub-agent inventory confirmed this is the one real
collision). Surface both as columns:

```python
# was: {rollout_index, month_index, series_id: Utf8, value: Float64}
SERIES_LEVELS_SCHEMA = SERIES_VALUES_SCHEMA = pl.Schema({
    "rollout_index": pl.Int64(), "month_index": pl.Int64(),
    "kind": pl.Utf8(),        # LevelSeriesKind StrEnum: inflation|sp500|crypto|home_value|rent
    "qualifier": pl.Utf8(),   # null for inflation/sp500; symbol for crypto; location_id for home_value/rent
    "value": pl.Float64(),
})
# filter: .filter((pl.col("kind") == LevelSeriesKind.CRYPTO) & (pl.col("qualifier") == "btc"))
# join keys become ["rollout_index","month_index","kind","qualifier"]
```

`(kind, qualifier)` IS the typed key — reconstructed by a `match`, never by
prefix-splitting. The `asset_id` column splits the same way into
`asset_kind` + `asset_qualifier`.

## Typed-key API: `(kind, qualifier)` replaces `wire_id` / `parse_*`

In `augur/model/series.py` and `augur/product/asset_key.py`:

- `LevelSeriesKind` / `AssetKind`: `IntEnum` → **`StrEnum`** (verified pure
  discriminators; the column stores the enum value, e.g. `"crypto"`).
  `PrivateEquityRegimeCode` / `PrivateEquityEventKindCode` stay `IntEnum` (real
  numeric codes in frames + `sample_sanity.yaml`).
- Each key variant gains a `qualifier: str | None` property (None / symbol /
  location_id / issuer_id).
- Module factories `level_series_key_from_columns(kind, qualifier)` and
  `asset_key_from_columns(kind, qualifier)` (`match` on kind) replace the
  `parse_*` functions.
- Keep a display-only `__str__` (renders `kind` or `kind:qualifier`) for
  log/error messages — never parsed back.

`LevelSeriesGroups[ValueT]` — new `model/level_series_groups.py`, `FrozenModel`,
`Generic[ValueT]`, fields `inflation/sp500: ValueT|None`, `crypto:
dict[CryptoSymbol, ValueT]`, `home_value/rent: dict[LocationId, ValueT]`, plus
`by_level_key() -> dict[LevelSeriesKey, ValueT]`. `extra="forbid"` ⇒ a stray
`"crypto:btc"` key fails at load. Reused by config surfaces 1/4/6.

---

## Phase 1 — Config (the original task)

Typed discriminated unions in YAML; `series` collapsed under `independent`.
Internally still emits frame wire strings (keeps `parse_*` alive for now).
Independently valuable + landable.

| #   | File / class                                                                    | New shape                                                                                                                                                                                            |
| --- | ------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `model/independent_exogenous.py` `IndependentExogenousProviderConfig`           | inherit `LevelSeriesGroups[ScalarSeriesSpec]` (per-kind fields, no `series:` wrapper) + `type` + `private_equity_marks: dict[IssuerId, ScalarSeriesSpec]`; drop `_classified_series` prefix re-parse |
| 2   | `model/series_model.py` `IndependentSeriesModels` (sim/bench twin)              | same collapse                                                                                                                                                                                        |
| 3   | `api/portfolio.py` `HoldingPositionConfig`                                      | `value_series: AssetKey` typed in YAML; drop `value_series_id` + `asset_key` re-parse; `_validate_references`/`level_anchors`/`to_initial_lots` consume the typed key                                |
| 4   | `model/conditioning.py` `ExogenousConditioningContext`                          | `LevelSeriesGroups[tuple[ExogenousObservedPoint, ...]]` + `start_at`; `NormalizedObservation.key: LevelSeriesKey`                                                                                    |
| 5   | `model/location_series_sources.py` `LocationSeriesSourcesConfig`                | `home_value/rent: dict[LocationId, LocationId]` (consumer rebuilds the key; `state_space.py:441` already does this)                                                                                  |
| 6   | `model/vecm.py` `VecmExogenousProviderConfig` / `VecmModel.latest_observations` | level entries via `LevelSeriesGroups[float]`; blob-aux keys (`spy_adjusted_close_latest`, `*_by_factor`) modeled explicitly                                                                          |
| 7   | `model/sample_sanity.py` checks                                                 | `key: LevelSeriesKey`, `required_level_series: tuple[LevelSeriesKey,...]`, `issuer_id: IssuerId`                                                                                                     |

Plus: ducktape test configs/fixtures (`sim/simulate_test.py` ~40,
`sim/test_rental_lifecycle_e2e.py` ~20, `model/*_test.py`, `fit/*`,
`api/testdata/config.yaml`). Then gaffer: repin + migrate
`exogenous_provider.yaml`, `sample_sanity.yaml`, `k8s/augur/config.yaml`,
`config/train.yaml`; resolve the pre-existing `config_test.py` import of
`private_equity_level_series_ids` / `private_equity_sale_event_id`.

End state: config carries zero prefixes; frames/artifacts/API still do.

## Phase 2 — In-memory polars frames

Replace prefix columns with `kind`+`qualifier`. Sub-agent inventory (~11 files
beyond the schema defs):

| #   | Schema / column                                                                    | Change                                                                                                                                                                                                                                                          |
| --- | ---------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 8   | `model/exogenous.py` `SERIES_LEVELS_SCHEMA` / `SERIES_VALUES_SCHEMA` (`series_id`) | → `kind`,`qualifier`. Rewrite `series_levels_frame`, `series_values_from_bundle`, `level_matrix`, `validate_sample_satisfies_request`, `anchor_sampled_series_levels` (join keys), `parse_levels_frame_keys` → `level_keys_in_frame`, `_matrix_from_long_frame` |
| 9   | `sim/external_series.py` `SERIES_EVENTS_SCHEMA` (`event_id`)                       | events are a "legacy holdover" (PE tender moved to a bundle channel) — **confirm dead and delete the frame**; else → `kind`,`qualifier`                                                                                                                         |
| 10  | `sim/codec/assets.py` `asset_id` column                                            | → `asset_kind`,`asset_qualifier`; rewrite producers (`decode_asset_lots`, `decode_pe_*`)                                                                                                                                                                        |

Sim-engine sweep (same phase): `sim/compiler/plan.py` (series/event index maps,
`lot_asset_series_index`, `_reject_missing_property_sale_home_values`),
`sim/compiler/series.py` (`collect_series_ids`, `external_values_cube`,
`external_event_values_cube`), `sim/projections.py` (`TRANSACTION_SCHEMA`
`asset_id` + the asset_lots↔series_values join ~382), `sim/state.py`
(`ASSET_LOT_FRAME` `asset_id`), `sim/events.py` (event-frame `asset_id`
columns), `product/decode.py` (group_by/filter/wire-build on `asset_id`),
`model/composite_exogenous.py` (`_reject_duplicate_ids` on `series_id`).
Update all frame test data.

The sim string-interning table (`series_index_by_id`) keys on the wire string
today; it becomes keyed on `(kind, qualifier)` tuples (or a small frozen
`LevelSeriesKey`/`AssetKey`). No external artifact churn in this phase.

End state: no prefix strings in memory; artifacts + API still carry them.

## Phase 3 — On-disk trained artifacts (forces regeneration)

| #   | Artifact                                              | Change                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| --- | ----------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 11  | `model/state_space.py` `StateSpaceModelArtifact` JSON | `factor_names: tuple[str,...]` → `factors: tuple[FactorKey,...]` (typed union: level variants + PE-mark variant w/ `issuer_id`; serializes `{kind,qualifier}`). The wire-keyed `dict[str,float]` maps (`latest_level_by_factor`, `monthly_log_return_mu`, `filtered_log_state_mean`, PE-prior dicts) → **positional** `tuple[float,...]` aligned to `factors` (cov is already positional). `_classify_factor`/`_series_factor_map` consume typed factors |
| 12  | `model/vecm.py` `.npz` blob                           | replace the `factor_names` object-array with parallel `factor_kinds`+`factor_qualifiers` arrays; bump blob schema version. `save()`/`load()` at `vecm.py` ~352/376                                                                                                                                                                                                                                                                                       |

Regenerate checked-in blobs via the fit/`save()` paths: ducktape
`augur/model/testdata/fixture_*.json`, `augur/fit/calibrated/trained_vecm.npz`
(+ `trained_vecm_provider.yaml`); gaffer `state_space_*_artifact.json`,
`openai_private_equity_model.json`, and the `trained_vecm.npz` baked into the
augur OCI image (rebuild that image layer).

End state: nothing on disk carries prefixes.

## Phase 4 — API wire + final deletion + CI guard

| #   | Field                                                                                                                                           | Change                                                                                                                                                                                                                                                                                                                                                             |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 13  | `product/wire.py` `asset_id: str` (`HoldingSaleEvent`, `PrivateEquityMarkerEvent`, `PrivateEquityOpportunityEvent`) + `spend_index: SpendIndex` | typed `AssetKey` / `LevelSeriesKey` reference (serializes `{kind,qualifier}`). Update `product/decode.py` wire construction + `api/server_test.py` assertions. **Verify the frontend** — sub-agent found no TS refs to `series_id`/`asset_id`, but confirm `asset_id`/`spend_index` consumption before changing the shape (the one external/JSON-breaking surface) |

Then: **delete** `wire_id`, `parse_level_series_key`, `try_parse_level_series_key`,
`parse_asset_key`, `try_parse_asset_key`. Add the CI grep guard (Definition of
done). Final `rg` sweep for any `:`-splitting on series/asset ids.

End state: zero magic-prefix strings; guard prevents regressions.

---

## Per-phase sequencing (each: ducktape first, then gaffer)

1. ducktape branch `claude/determined-darwin-TLzqj`: land the phase's commits,
   each green via `bazelisk --server_javabase=$JAVA_HOME … --config=rbe test`.
2. push ducktape.
3. gaffer: bump `archive_override` pin to the new ducktape commit
   (`nix-prefetch-url --print-path …/<commit>.tar.gz` + integrity), migrate the
   phase's gaffer surfaces, green, push.

## Operational prerequisites (verified)

- **System Java 21** at `/usr/lib/jvm/java-21-openjdk-amd64`; startup flag:
  `bazelisk --server_javabase=$JAVA_HOME build … --config=rbe`
  (`--server_javabase` is startup-only, not a build flag).
- **BuildBuddy key**: `sops -d --extract '["buildbuddy_api_key"]'
secrets/buildbuddy.yaml`; export `BUILDBUDDY_API_KEY` (also wired into
  `~/.config/bazel/buildbuddy.bazelrc` by `setup_buildbuddy.sh`).
- **Use local `bazelisk --config=rbe`, NOT `bbr`.** `bbr` is broken on this
  branch: git-state mirroring fails applying a pre-existing binary PNG in commit
  `7af7a75` (branch is 34 commits ahead of `origin/devel`). Local
  `bazelisk --config=rbe` dispatches to RBE without mirroring — verified green
  (invocation `835356ed…`).
- **Commits** run under `nix develop` so `pre-commit` + `SOPS_AGE_KEY` resolve.

## Risks / open items

- **Phase 4 API + frontend** is the only external-breaking surface; verify
  frontend consumption before changing wire shape.
- **Phase 3 artifact regen** must have a clean fit→`save()` path; the vecm `.npz`
  is baked into the augur OCI image (rebuild needed).
- **Phase 2 two-column joins** in the sim hot path — watch for perf regression
  vs the single-string join/intern.
- **SERIES_EVENTS_SCHEMA** is likely fully dead — prefer deleting over retyping.
- **gaffer `config_test.py`** imports symbols absent from both local and pinned
  ducktape — resolved in Phase 1's gaffer step.
