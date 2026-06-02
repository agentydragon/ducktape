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

**Landed so far:**

- `IndependentExogenousModel` holds level specs as a per-kind
  `LevelSeriesGroups[ScalarSeriesSpec]` (not a flattened `dict[LevelSeriesKey,
…]`), mirroring config / the sampled bundle. Added `LevelSeriesGroups.level_groups()`.
- `SeriesIndexedAmount.series_id: str` → `series: LevelSeriesKey` (rent/inflation
  amount index is now a typed key). The string consumers (`amount_arrays`,
  `simulate.py` validation, `collect_series_ids`) read `series.wire_id` as a
  temporary shim because the intern table / frame are still wire-string keyed.

### Magisteria — the structuring principle for Phase 2 (DECIDED)

The flat `series_id` string bag conflates three **disjoint** concerns. Code
audit (`engine/phases.py` — the only consumer of `external_values`) confirms
every non-PE series belongs to exactly one, defined by **what references it**:

| Magisterium        | Member kinds              | Referenced by      | Mechanic                                                    | Forbidden nonsense             |
| ------------------ | ------------------------- | ------------------ | ----------------------------------------------------------- | ------------------------------ |
| **Asset price**    | `sp500`, `crypto`         | a holding/lot      | `value = qty × price[t]` (absolute)                         | a lot denominated in inflation |
| **Property value** | `home_value:<loc>`        | a property         | `value = price × hv[t]/hv[base]` (ratio)                    | a property priced by crypto    |
| **Index**          | `inflation`, `rent:<loc>` | a recurring amount | `amt = base × idx[t]/idx[base]` (ratio)                     | rent escalated by sp500        |
| _(Private equity)_ | _PE issuers_              | a PE lot           | _typed `PrivateEquityBundle` (already its own magisterium)_ | —                              |

The three reference sources are already disjoint in code (`lot.asset_id`,
`property.location_id`, `amount.series`) — nothing crosses. Phase 2 makes that
de-facto separation **type-enforced** so the cross-wirings are unrepresentable.
This is the same move `PrivateEquityBundle` already made; we give the other
groupings the same first-class treatment.

**Type design** — narrow the reference unions to their magisterium:

```python
# augur/model/series.py
type AssetPriceKey    = SP500Key | CryptoKey           # prices a lot (non-PE)
type PropertyValueKey = HomeValueKey                   # values a property
type IndexSeriesKey   = InflationKey | RentKey          # escalates an amount
# LevelSeriesKey stays the SUM — the model/sample layer still works over all
# non-PE level series uniformly (a sampler is asked for "these level series"):
type LevelSeriesKey   = AssetPriceKey | PropertyValueKey | IndexSeriesKey
```

Reference fields narrow (cross-wiring → mypy error):

- `SeriesIndexedAmount.series: IndexSeriesKey` (tightens the just-landed `series`).
- lot price ref → `AssetPriceKey` (`_level_key_from_asset_key` already returns this).
- property value ref → `PropertyValueKey` (`HomeValueKey`).

**Model bundle — magisteria, not bare per-kind.** `SampledExogenousBundle`
groups by magisterium, each a sub-bundle; per-kind frames live _inside_ a
magisterium where a magisterium spans >1 kind:

```python
@dataclass(frozen=True)
class SampledExogenousBundle:
    asset_prices: AssetPriceFrames      # sp500 (scalar) + crypto (symbol-keyed)
    property_values: pl.DataFrame       # home_value, location-keyed (single kind)
    index_levels: IndexFrames           # inflation (scalar) + rent (location-keyed)
    private_equity: PrivateEquityBundle # unchanged
    metadata: Mapping[str, object]
```

**Split the sample/request layer too (DECIDED).** `ExogenousSamplingRequest`'s
single `required_level_series: frozenset[LevelSeriesKey]` fragments into three
magisterium channels alongside the existing PE channel:

```python
required_asset_prices: frozenset[AssetPriceKey]       = frozenset()
required_property_values: frozenset[PropertyValueKey] = frozenset()
required_index_series: frozenset[IndexSeriesKey]      = frozenset()
required_private_equity_issuers: frozenset[IssuerId]  = frozenset()
```

Each provider's `sample()` produces/validates per magisterium, and the bundle
exposes typed read accessors per magisterium (`asset_price_matrix(AssetPriceKey)`,
`property_value_matrix(PropertyValueKey)`, `index_matrix(IndexSeriesKey)`).
`LevelSeriesKey` remains the **sum** only as an internal convenience where a
single helper genuinely ranges over all non-PE level series (e.g. the
`LevelSeriesGroups` config shape); the runtime sample/consume path is fully
magisterium-typed.

### Staged landing (each commit green on RBE before the next)

- **A.** Narrowed reference union types in `series.py` (`AssetPriceKey` /
  `PropertyValueKey` / `IndexSeriesKey`; `LevelSeriesKey` = their sum) + narrow
  the reference fields (`SeriesIndexedAmount.series: IndexSeriesKey`,
  `_level_key_from_asset_key -> AssetPriceKey`). Purely additive narrowing.
- **B.** `SampledExogenousBundle` → magisterium sub-bundles + per-magisterium
  builders / anchor / validate / accessors; update all ~10 producers + model
  consumers. Request stays the sum here (validate routes), so B is self-contained.
- **C.** Split `ExogenousSamplingRequest` into the three magisterium channels +
  update every builder (`product/scenarios.required_level_series`, composite,
  `sample_sanity`, testing, vecm) and `validate_sample_satisfies_request`.
- **D1.** ✅ Deleted the structurally-dead `series_events` / `event_id` /
  `external_event_*` frame (never populated, never read).
- **D2 (= D + Phase 4, merged — DECIDED "type everything incl. output + API"). ✅ done.**
  Typed the sim's asset/series identity end to end, with the API wire riding along:
  - **D2a** ✅ — typed scenario fields: `InitialLot.asset` / `ScheduledAssetSale.asset:
AssetKey`, `LiquidityPolicy.asset_preference_chain: list[AssetKey]` (dropped the
    `try_parse_asset_key` `.asset` property; the field IS the typed key). The lot→price
    mapper landed as `asset_key.asset_price_key` (not `model/series.py` — that would be a
    circular import; `asset_key` already imports `series` + is imported by the sim).
    Migrated every constructor incl. bench_scenario + the bare-string engine tests
    (`asset_id="btc"`/`"ixus"` → `CryptoAssetKey(symbol=...)`; both fixed-price sales, so
    no series-fixture coordination). `api/portfolio.to_initial_lots` passes
    `asset=position.value_series` straight through.
  - **D2b** ✅ — typed series intern: `series_index_by_id: dict[LevelSeriesKey,int]` through
    all 7 compile functions; `collect_level_series_keys`; `CompiledSimulation.series_keys`;
    `product/decode` rebuilds typed. The external frame stays flat (per "Sim storage stays
    flat") — the string↔typed bridge is localized to two boundary sites (collect parse + the
    cube wire_id index map), removed when the frame itself goes typed.
  - **D2c** ✅ — typed asset intern: dedicated `AssetTable` (`list[AssetKey]` +
    `dict[AssetKey,int]`) for lot/sale/chain codes alongside `StringTable`;
    `CompiledSimulation.assets`. PE-guard `lot_asset_series_index` (PE prices via
    `pe_channels`). codec lifts codes → wire ids via `codes_to_asset_wire_ids` (the
    `asset_id` output column + cause-ids stay wire strings for the frontend);
    `product/decode._lot_value_by_month` reads `plan.assets[code]` typed.
  - **D2d** ✅ (satisfied by D2b+c) — the projection asset↔series pricing join
    (`asset_lots.asset_id == series_values.series_id`) is now a value-equality join on wire
    ids that are _provably typed-derived_ (both produced via `.wire_id` from typed identity).
    It does no prefix dispatch (`startswith`/`partition`), so it already satisfies the
    endpoint guard; reshaping the internal frames to `(kind,sub_id)` columns or a cube-index
    join buys nothing externally and fights "Sim storage stays flat", so it's deferred to the
    eventual full frame-schema retype (Phase 4) rather than forced here.
  - **D2e** ✅ — `product/wire.py` `HoldingSaleEvent`/`PrivateEquity*Event` carry
    `asset: AssetKey`; `asset_id` is a `@computed_field` deriving `asset.wire_id`, so the
    frontend's `event.assetId` fallback (`data_helpers.js`) is unchanged. `spend_index`
    stays a `Literal` policy flag, out of scope.
- **E.** (partial ✅) Deleted now-dead `try_parse_asset_key`. `parse_asset_key` /
  `parse_level_series_key` / `try_parse_level_series_key` / `wire_id` remain load-bearing at
  the flat-frame + output-column boundaries (decode parse, collect parse, cube bridge,
  `asset_id`/cause-id columns, computed wire `asset_id`); they're deleted when Phase 3/4
  retypes the frames + artifacts.

**Sim storage stays flat.** The engine reads one numeric cube
`external_values[idx, rollout, month]` via dense int indices; nothing reads the
frame by kind. The intern table is typed (`dict[LevelSeriesKey | AssetKey,
int]`) and the `projections.py` asset↔series join keyed on typed identity, but
the cube and `ExternalSeriesContext` remain one flat working frame — splitting
sim storage by magisterium buys nothing and fans out the pricing join.

**Dead frame:** `series_events` / `event_id` is structurally dead (no producer
ever populates it; PE tender moved to the bundle). Delete it in this phase.

Replace the one prefix-keyed frame with magisterium sub-bundles (no `kind`
column — the frame/field is the kind; rows carry only their sub-id). Sub-agent
inventory (~11 files beyond the schema defs):

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
