# Kill magic-prefix wire strings everywhere in augur

## Goal

Augur encodes the _kind_ of a series/asset in a magic string prefix
(`"crypto:btc"`, `"home_value:san_francisco_ca"`, `"rent:vallejo_ca"`,
`"private_equity:openai"`) and dispatches on it. The runtime typed boundary
(`LevelSeriesKey`, `AssetKey`) already exists, but prefixes still survive in
**config**, in **in-memory polars frames**, in **on-disk trained artifacts**, and
in one **API field**. Remove the prefix from ALL of them: identity is carried
structurally (typed unions in config/API; `kind` + `qualifier` columns in
frames; typed factor records in artifacts), never as a parseable string.

This is the columnar/serialized analog of the existing `PrivateEquityBundle`,
which already carries PE state as a bare `issuer_id` column + typed channel
columns rather than magic-prefixed rows.

## Design principle (REVERSED from the first draft)

- **No magic-prefix string is carried anywhere** — not config, not polars
  columns, not artifact JSON/npz, not the API. The earlier "serialization
  boundaries keep wire strings" rule is dropped per explicit decision.
- **Identity is structural.** A `LevelSeriesKey`/`AssetKey` decomposes into a
  `kind` discriminator + a `qualifier` payload (symbol / location_id /
  issuer_id; `None` for singletons). Columns/records carry that pair.
- **No backcompat.** Old configs/artifacts/frames must fail loudly. gaffer-private
  may be red between the ducktape push and the gaffer migration — accepted.

## Chosen frame shape: Option 1 — tag + payload columns

The prefix encodes TWO things (kind discriminator + sub-id), so a single bare
string cannot replace it (`san_francisco_ca` would collide between `home_value`
and `rent`). Surface both as columns:

```python
# was: {rollout_index, month_index, series_id: Utf8, value: Float64}
SERIES_LEVELS_SCHEMA = SERIES_VALUES_SCHEMA = pl.Schema({
    "rollout_index": pl.Int64(), "month_index": pl.Int64(),
    "kind": pl.Utf8(),        # LevelSeriesKind StrEnum value: inflation|sp500|crypto|home_value|rent
    "qualifier": pl.Utf8(),   # null for inflation/sp500; symbol for crypto; location_id for home_value/rent
    "value": pl.Float64(),
})
# filter: .filter((pl.col("kind") == LevelSeriesKind.CRYPTO) & (pl.col("qualifier") == "btc"))
```

`(kind, qualifier)` IS the typed key — reconstructed by a `match`, never by
prefix-splitting.

## Typed-key API: replace `wire_id` / `parse_*` with `(kind, qualifier)`

In `augur/model/series.py` and `augur/product/asset_key.py`:

- `LevelSeriesKind` / `AssetKind`: `IntEnum` → **`StrEnum`** (verified pure
  discriminators; the column now stores the enum value, e.g. `"crypto"`). PE
  `PrivateEquityRegimeCode` / `PrivateEquityEventKindCode` stay `IntEnum` (real
  numeric codes).
- Add to each key variant: `qualifier: str | None` property (None / symbol /
  location_id / issuer_id).
- Add module factories: `level_series_key_from_columns(kind, qualifier)` and
  `asset_key_from_columns(kind, qualifier)` — `match` on kind.
- **Delete** `wire_id`, `parse_level_series_key`, `try_parse_level_series_key`,
  `parse_asset_key`, `try_parse_asset_key`. Keep only a `__str__` (renders
  `kind` or `kind:qualifier`) for **diagnostics/log/error messages** — never
  parsed back. This is the only "stringish" remnant and it is display-only.

## Surfaces

### A. Config (typed discriminated unions; `series` collapsed under `independent`)

| #   | File / class                                                                    | New shape                                                                                                                                                 |
| --- | ------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `model/independent_exogenous.py` `IndependentExogenousProviderConfig`           | inherit `LevelSeriesGroups[ScalarSeriesSpec]` (per-kind fields, no `series:` wrapper) + `type` + `private_equity_marks: dict[IssuerId, ScalarSeriesSpec]` |
| 2   | `model/series_model.py` `IndependentSeriesModels` (sim/bench twin)              | same collapse                                                                                                                                             |
| 3   | `api/portfolio.py` `HoldingPositionConfig`                                      | `value_series: AssetKey` (typed in YAML); drop `value_series_id`/`asset_key` re-parse                                                                     |
| 4   | `model/conditioning.py` `ExogenousConditioningContext`                          | `LevelSeriesGroups[tuple[ExogenousObservedPoint, ...]]` + `start_at`; `NormalizedObservation.key: LevelSeriesKey`                                         |
| 5   | `model/location_series_sources.py` `LocationSeriesSourcesConfig`                | `home_value/rent: dict[LocationId, LocationId]`                                                                                                           |
| 6   | `model/vecm.py` `VecmExogenousProviderConfig` / `VecmModel.latest_observations` | typed; level entries via `LevelSeriesGroups[float]`; blob-aux keys modeled explicitly                                                                     |
| 7   | `model/sample_sanity.py` checks                                                 | `key: LevelSeriesKey`, `required_level_series: tuple[LevelSeriesKey,...]`, `issuer_id: IssuerId`                                                          |

`LevelSeriesGroups[ValueT]` — new `model/level_series_groups.py`, `FrozenModel`,
`Generic[ValueT]`, fields `inflation/sp500: ValueT|None`, `crypto:
dict[CryptoSymbol, ValueT]`, `home_value/rent: dict[LocationId, ValueT]`, plus
`by_level_key() -> dict[LevelSeriesKey, ValueT]`. `extra="forbid"` ⇒ a stray
`"crypto:btc"` key fails at load.

### B. In-memory polars frames (Option 1: kind + qualifier)

| #   | Schema / column                                                                    | Change                                                                                                                                                                                                                                  |
| --- | ---------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 8   | `model/exogenous.py` `SERIES_LEVELS_SCHEMA` / `SERIES_VALUES_SCHEMA` (`series_id`) | → `kind`, `qualifier`. Rewrite `series_levels_frame`, `level_values`, `level_series_ids`, `required_level_series_from_frame`, `series_values_from_bundle`.                                                                              |
| 9   | `sim/external_series.py` `SERIES_EVENTS_SCHEMA` (`event_id`)                       | → `kind`, `qualifier` (or **drop the frame** — events are a "legacy holdover", PE tender moved to a bundle channel; confirm dead first, prefer deletion).                                                                               |
| 10  | `sim/codec/assets.py` `asset_id` column                                            | → `asset_kind`, `asset_qualifier`. Rewrite `asset_ids_to_issuer_ids`, `primary_asset_ids`, `is_private_equity_asset`, `asset_id_column`, `asset_kind_label` to take/return typed `AssetKey` / the two columns. `ASSET_ID_COLUMN` split. |

**Sim engine sweep (same commit as #8/#10):** the join from `series_values` to a
holding's value-series, and any `asset_id`/`series_id` reference in
`sim/compiler/plan.py`, `sim/runtime.py`, `sim/slice.py`, `sim/tax.py`,
`sim/projections.py`, `product/decode.py`, `product/scenarios.py`, becomes a
two-column `(kind, qualifier)` match. Enumerate exact call sites at impl time
(rg `series_id|asset_id|event_id`).

### C. On-disk trained artifacts (forces regeneration)

| #   | Artifact                                                   | Change                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| --- | ---------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 11  | `model/state_space.py` `StateSpaceModelArtifact` JSON      | `factor_names: tuple[str,...]` → `factors: tuple[FactorKey, ...]` (typed union: level-series variants + a PE-mark variant carrying `issuer_id`; serializes as `{kind, qualifier}`). The four wire-keyed `dict[str,float]` maps (`latest_level_by_factor`, `monthly_log_return_mu`, `filtered_log_state_mean`, + PE-prior dicts) → **positional** `tuple[float,...]` aligned to `factors` (covariance is already positional), so NO string keys remain. `_classify_factor`/`_series_factor_map` consume typed factors. |
| 12  | `model/vecm.py` `.npz` blob (`VECM_BLOB_FACTOR_NAMES_KEY`) | replace the `factor_names` string array with two parallel arrays `factor_kinds` + `factor_qualifiers`; bump `VECM_BLOB_SCHEMA_VERSION_KEY`. Messiest item.                                                                                                                                                                                                                                                                                                                                                            |

**Artifact regeneration** (schema change invalidates checked-in blobs):

- ducktape testdata (`augur/model/testdata/fixture_*.json`,
  `augur/fit/calibrated/trained_vecm_provider.yaml` + its `.npz`): regenerate via
  the fit/`save()` paths.
- gaffer artifacts (`state_space_macro_artifact.json`, `state_space_artifact.json`,
  `openai_private_equity_model.json`) + the `trained_vecm.npz` baked into the
  augur OCI image: regenerated in the gaffer phase.

### D. API

| #   | Field                                    | Change                                                                                                                                                                              |
| --- | ---------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 13  | `product/wire.py:127` `spend_index: str` | → typed `LevelSeriesKey` reference. Check `product/scenarios.py` + whether the frontend sends `spend_index` (no TS refs to `series_id`/`asset_id` found, but verify `spend_index`). |

## Commit plan (ducktape, branch `claude/determined-darwin-TLzqj`)

Each self-consistent and green (`bazelisk --server_javabase=$JAVA_HOME … --config=rbe test`):

1. Typed-key core: `StrEnum` flip + `qualifier` props + `*_from_columns`
   factories; keep `wire_id`/`parse_*` temporarily so the tree still builds.
2. `level_series_groups.py` generic + test.
3. Config surfaces 1–2 (provider + twin) + sim consumers + test configs.
4. Config surface 3 (portfolio `value_series`) + `api/testdata/config.yaml`.
5. Config surfaces 4–7 (conditioning / location / vecm config / sample_sanity).
6. Frame surfaces 8–10 + sim-engine sweep + frame test data.
7. Artifact surface 11 (state_space) + regen ducktape testdata.
8. Artifact surface 12 (vecm .npz) + regen ducktape vecm fixtures.
9. API surface 13 (`spend_index`).
10. Delete `wire_id` + `parse_*` (now unused); final `rg` for any prefix/`:`
    splitting; docs/docstrings refresh (`series.py`, `asset_key.py`,
    `private_equity_bundle.py`, `exogenous.py`, `external_series.py`).

Push branch.

## Gaffer migration (after ducktape pushed)

1. Bump `archive_override` pin in gaffer `MODULE.bazel` to the new ducktape
   commit (repin: `nix-prefetch-url --print-path …/<commit>.tar.gz` + integrity).
2. Migrate gaffer YAML to typed shapes: `exogenous_provider.yaml` (conditioning
   observations, location_series_sources), `sample_sanity.yaml`,
   `k8s/augur/config.yaml` (portfolio `value_series`, provider, `spend_index`),
   check `config/train.yaml`.
3. **Regenerate gaffer trained artifacts** (state_space JSONs, PE model JSON,
   `trained_vecm.npz`) under the new schema; rebuild the augur OCI image layer
   that bakes the npz.
4. Resolve the pre-existing `gaffer_augur/config_test.py` import of
   `private_equity_level_series_ids` / `private_equity_sale_event_id` (exist in
   neither local ducktape nor the pinned commit) — rewrite against the typed API.
5. `nix develop` + `pre-commit` + `bazelisk … --config=rbe test //...` green; push.

## Operational prerequisites (verified)

- **System Java 21** at `/usr/lib/jvm/java-21-openjdk-amd64`; pass as a startup
  flag: `bazelisk --server_javabase=$JAVA_HOME build … --config=rbe`
  (`--server_javabase` is startup-only, NOT a build flag).
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

- **Artifact regeneration is the biggest risk** (#11/#12): changing on-disk
  schema invalidates every checked-in trained blob; the vecm `.npz` is baked into
  the augur OCI image, so the image layer must be rebuilt. Confirm a clean
  regen path (fit entrypoints) before touching schemas.
- **SERIES_EVENTS_SCHEMA (#9)** may be fully dead — prefer deleting the frame
  over retyping it; confirm no engine path still reads it.
- **Two-column joins** in the sim compiler (#8/#10) — verify no perf-sensitive
  hot path regresses vs the single-string join.
- **`spend_index` frontend** (#13) — confirm whether the frontend emits it before
  changing the API type.
- **gaffer `config_test.py`** import discrepancy — resolved in gaffer phase.
