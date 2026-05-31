# Kill magic-prefix wire strings everywhere in augur (staged)

## Goal

Augur encodes the _kind_ of a series/asset in a magic string prefix
(`"crypto:btc"`, `"home_value:san_francisco_ca"`, `"rent:vallejo_ca"`,
`"private_equity:openai"`) and dispatches on it (`.startswith` / `partition(":")`).
The runtime typed boundary (`LevelSeriesKey`, `AssetKey`) already exists, but
prefixes still survive in **config**, **in-memory polars frames**, **on-disk
trained artifacts**, and the **API wire**. The endpoint is zero magic-prefix
strings anywhere — identity carried structurally (typed unions in config/API;
per-kind frames carrying only a sub-id column; typed factor records in artifacts).

This is the columnar/serialized analog of the existing `PrivateEquityBundle`,
which already is its own frame carrying a bare `issuer_id` column + typed channel
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

## Frame shape decision: per-kind frames (no `kind` column)

Decided: each level-series kind is its **own frame**, so the frame's identity IS
the kind — there is no `kind` column at all, and a row carries only its sub-id
(symbol / location_id), or nothing for singletons. This mirrors how
`PrivateEquityBundle` is already its own frame, and makes the sampled bundle line
up field-for-field with config's `LevelSeriesGroups`. Invalid states (an
`inflation` row with a `location_id`, or the `home_value`/`rent`
`san_francisco_ca` collision the inventory flagged) become unrepresentable.

Three schemas, grouped by SHAPE; five bundle fields (the field name carries the
kind, so `home_value` and `rent` are distinct frames despite a shared schema):

```python
SCALAR_LEVELS_SCHEMA   = pl.Schema({"rollout_index": Int64, "month_index": Int64, "value": Float64})                       # inflation, sp500
SYMBOL_LEVELS_SCHEMA   = pl.Schema({"rollout_index": Int64, "month_index": Int64, "symbol": Utf8, "value": Float64})       # crypto
LOCATION_LEVELS_SCHEMA = pl.Schema({"rollout_index": Int64, "month_index": Int64, "location_id": Utf8, "value": Float64})  # home_value, rent

@dataclass(frozen=True)
class SampledExogenousBundle:
    inflation: pl.DataFrame        # SCALAR   — no id column
    sp500: pl.DataFrame            # SCALAR
    crypto: pl.DataFrame           # SYMBOL   — keyed by symbol
    home_value: pl.DataFrame       # LOCATION — keyed by location_id
    rent: pl.DataFrame             # LOCATION
    private_equity: PrivateEquityBundle   # already its own frame today
    metadata: Mapping[str, object] = field(default_factory=dict)
```

Lookups dispatch on the typed key to the right frame + sub-id column (no string
filter for singletons):

```python
match key:
    case InflationKey() | SP500Key():    frame = getattr(bundle, key.kind)            # no filter
    case CryptoKey(symbol=s):            frame = bundle.crypto.filter(pl.col("symbol") == str(s))
    case HomeValueKey(location_id=loc) | RentKey(location_id=loc):
        frame = getattr(bundle, key.kind).filter(pl.col("location_id") == str(loc))
```

The sim's `asset_id` (lots/events) splits the same way: a `crypto` lot frame
keyed by `symbol`, a `private_equity` lot frame keyed by `issuer_id`. The sim's
internal dense `external_values` cube is unchanged (flat numeric
`(series, rollout, month)` + index map); only how that map is BUILT changes —
from the typed per-kind frames, not parsed strings.

## Typed-key API: `kind` discriminator + per-key sub-id (no `parse_*`)

In `augur/model/series.py` and `augur/product/asset_key.py`:

- `LevelSeriesKind` / `AssetKind`: `IntEnum` → **`StrEnum`**. Frames no longer
  carry a `kind` column, but the StrEnum value is still the discriminator that
  serializes in the spots that ARE Pydantic-serialized: config `sample_sanity`
  `key:`, portfolio `value_series:`, and the API wire (`asset`, `spend_index`) —
  human-readable `kind: crypto`. `PrivateEquityRegimeCode` /
  `PrivateEquityEventKindCode` stay `IntEnum` (real numeric codes).
- Each key already carries its sub-id as a typed field (`CryptoKey.symbol`,
  `HomeValueKey.location_id`, …); add a `kind` convenience that equals the bundle
  field name. No unified `qualifier` field is needed (per-kind frames). A
  display-only `__str__` (`kind` or `kind:sub_id`) stays for logs — never parsed.
- **Delete** `wire_id` and the `parse_*` functions. Per-kind frame readback
  constructs the specific key directly from its sub-id column
  (`CryptoKey(symbol=CryptoSymbol(s))`), never by splitting a prefix.

`LevelSeriesGroups[ValueT]` — new `model/level_series_groups.py`, `FrozenModel`,
`Generic[ValueT]`, fields `inflation/sp500: ValueT|None`, `crypto:
dict[CryptoSymbol, ValueT]`, `home_value/rent: dict[LocationId, ValueT]`, plus
`by_level_key() -> dict[LevelSeriesKey, ValueT]`. `extra="forbid"` ⇒ a stray
`"crypto:btc"` key fails at load. Reused by config surfaces 1/4/6. Note this is
the SAME five-field shape as `SampledExogenousBundle` above — config and the
sampled bundle line up field-for-field.

---

## Phase 1 — Config (the original task)

Typed discriminated unions in YAML; `series` collapsed under `independent`.
Internally still emits frame wire strings (keeps `parse_*` alive until Phase 2).
Independently valuable + landable. **Landed:** 1, 2, 3, 7 (the clean
config-with-prefix surfaces). **Deferred to Phase 3:** 4, 5, 6 — see note below.

| #   | File / class                                                                    | New shape                                                                                                                                                                                            | status    |
| --- | ------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------- |
| 1   | `model/independent.py` `IndependentExogenousProviderConfig`                     | inherit `LevelSeriesGroups[ScalarSeriesSpec]` (per-kind fields, no `series:` wrapper) + `type` + `private_equity_marks: dict[IssuerId, ScalarSeriesSpec]`; drop `_classified_series` prefix re-parse | ✅ done   |
| 2   | `model/series_model.py` `IndependentSeriesModels` (sim/bench twin)              | same collapse via `LevelSeriesGroups.from_level_keys`                                                                                                                                                | ✅ done   |
| 3   | `api/portfolio.py` `HoldingPositionConfig`                                      | `value_series: AssetKey` typed in YAML; drop `value_series_id` + `asset_key` re-parse; `_validate_references`/`level_anchors`/`to_initial_lots` consume the typed key                                | ✅ done   |
| 7   | `model/sample_sanity.py` checks                                                 | `key: LevelSeriesKey`, `required_level_series: tuple[LevelSeriesKey,...]`, `issuer_id: IssuerId`                                                                                                     | ✅ done   |
| 4   | `model/conditioning.py` `ExogenousConditioningContext`                          | observations keyed by trained-blob **factor** wire-ids (level series AND PE issuers, injected by `fit/state_space.py`); joins `state_space._series_factor_map`                                       | → Phase 3 |
| 5   | `model/location_series_sources.py` `LocationSeriesSourcesConfig`                | `home_value/rent: dict[LocationId, str]` where the **value** is a trained-blob factor id (`_location_factor`→`_factor_level`→`path_by_factor[...]`)                                                  | → Phase 3 |
| 6   | `model/vecm.py` `VecmExogenousProviderConfig` / `VecmModel.latest_observations` | `dict[str, Any]` heterogeneous data-provenance blob keyed by source names (`spy_adjusted_close_latest`, `housing_return_sources`, …); wire-ids only as inner `*_by_factor` keys                      | → Phase 3 |

**Why 4/5/6 deferred:** investigation showed these are not "config with a clean
level-series prefix" — their key/value identity is the **trained-artifact factor
name**, and the vecm `latest_observations` is a provenance map keyed by bespoke
source names, not level series. Typing them coherently requires typing factor
identity in the artifacts first (Phase 3's `StateSpaceModelArtifact` / vecm `.npz`
retype), so they ride along there rather than being forced now. The plan's
original Risks section already flagged surface 6 as the messiest and noted the
deployed gaffer config uses `state_space`, not `vecm`.

Plus: ducktape test configs/fixtures (`sim/simulate_test.py` ~40,
`sim/test_rental_lifecycle_e2e.py` ~20, `model/*_test.py`, `fit/*`,
`api/testdata/config.yaml`). Then gaffer: repin + migrate
`exogenous_provider.yaml`, `sample_sanity.yaml`, `k8s/augur/config.yaml`,
`config/train.yaml`; resolve the pre-existing `config_test.py` import of
`private_equity_level_series_ids` / `private_equity_sale_event_id`.

End state: config carries zero prefixes; frames/artifacts/API still do.

## Phase 2 — In-memory polars frames

Replace the one prefix-keyed frame with **per-kind frames** (no `kind` column —
the frame is the kind; rows carry only their sub-id). Sub-agent inventory
(~11 files beyond the schema defs):

| #   | Schema / column                                                                                 | Change                                                                                                                                                                                                                                                                                                                                                                       |
| --- | ----------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 8   | `model/exogenous.py` `SERIES_LEVELS_SCHEMA` / `SERIES_VALUES_SCHEMA` (single `series_id` frame) | → `SCALAR`/`SYMBOL`/`LOCATION` schemas + 5-field `SampledExogenousBundle`. Rewrite `series_levels_frame` → per-kind builders, `series_values_from_bundle`, `level_matrix` (match→frame), `validate_sample_satisfies_request`, `anchor_sampled_series_levels` (per-frame), `parse_levels_frame_keys` → `level_keys_in_bundle`, drop `_matrix_from_long_frame`'s string filter |
| 9   | `sim/external_series.py` `SERIES_EVENTS_SCHEMA` (`event_id`)                                    | events are a "legacy holdover" (PE tender moved to a bundle channel) — **confirm dead and delete the frame**; else split per-kind                                                                                                                                                                                                                                            |
| 10  | `sim/codec/assets.py` `asset_id` column                                                         | per-kind lot/event frames keyed by `symbol` / `issuer_id`; rewrite producers (`decode_asset_lots`, `decode_pe_*`)                                                                                                                                                                                                                                                            |

Sim-engine sweep (same phase): `sim/compiler/plan.py` (series/event index maps,
`lot_asset_series_index`, `_reject_missing_property_sale_home_values`),
`sim/compiler/series.py` (`collect_series_ids`, `external_values_cube`,
`external_event_values_cube`), `sim/projections.py` (`TRANSACTION_SCHEMA`
`asset_id` + the asset_lots↔series_values join ~382), `sim/state.py`
(`ASSET_LOT_FRAME` `asset_id`), `sim/events.py` (event-frame `asset_id`
columns), `product/decode.py` (group_by/filter/wire-build on `asset_id`;
`_is_private_equity` becomes an `AssetKind` compare, no parse),
`model/composite_exogenous.py` (`_reject_duplicate_ids` on `series_id`).
Update all frame test data.

The sim string-interning table (`series_index_by_id`) keys on the wire string
today; it becomes keyed on the typed `LevelSeriesKey` / `AssetKey`, built by
iterating each per-kind frame's sub-id column. No external artifact churn here.

End state: no prefix strings in memory; artifacts + API still carry them.

## Phase 3 — On-disk trained artifacts (forces regeneration)

| #   | Artifact                                              | Change                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| --- | ----------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| 11  | `model/state_space.py` `StateSpaceModelArtifact` JSON | `factor_names: tuple[str,...]` → `factors: tuple[FactorKey,...]` (typed union: level variants + PE-mark variant w/ `issuer_id`; serializes `{kind, sub-id}`). The wire-keyed `dict[str,float]` maps (`latest_level_by_factor`, `monthly_log_return_mu`, `filtered_log_state_mean`, PE-prior dicts) → **positional** `tuple[float,...]` aligned to `factors` (cov is already positional). `_classify_factor`/`_series_factor_map` consume typed factors |
| 12  | `model/vecm.py` `.npz` blob                           | replace the `factor_names` object-array with parallel `factor_kinds` + `factor_subids` arrays; bump blob schema version. `save()`/`load()` at `vecm.py` ~352/376                                                                                                                                                                                                                                                                                       |

Regenerate checked-in blobs via the fit/`save()` paths: ducktape
`augur/model/testdata/fixture_*.json`, `augur/fit/calibrated/trained_vecm.npz`
(+ `trained_vecm_provider.yaml`); gaffer `state_space_*_artifact.json`,
`openai_private_equity_model.json`, and the `trained_vecm.npz` baked into the
augur OCI image (rebuild that image layer).

End state: nothing on disk carries prefixes.

## Phase 4 — API wire + final deletion + CI guard

| #   | Field                                                                                                                                           | Change                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| --- | ----------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 13  | `product/wire.py` `asset_id: str` (`HoldingSaleEvent`, `PrivateEquityMarkerEvent`, `PrivateEquityOpportunityEvent`) + `spend_index: SpendIndex` | `asset: AssetKey` / `spend_index: LevelSeriesKey` — nested object, serializes `{"kind":"crypto","symbol":"btc"}` (the discriminated union, same repr as config typed-key spots). Update `product/decode.py` wire construction + `api/server_test.py` assertions. **Verify the frontend** — sub-agent found no TS refs to `series_id`/`asset_id`, but confirm `asset_id`/`spend_index` consumption before changing the shape (the one external/JSON-breaking surface) |

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
- **Phase 2 plumbing**: threading 5 per-kind frames (+ PE) instead of one frame
  is more wiring, but each piece is simpler (no per-row kind dispatch); watch the
  sim hot path for regressions vs the single-string intern/join.
- **SERIES_EVENTS_SCHEMA** is likely fully dead — prefer deleting over retyping.
- **gaffer `config_test.py`** imports symbols absent from both local and pinned
  ducktape — resolved in Phase 1's gaffer step.
