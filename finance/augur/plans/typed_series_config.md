# Typed series and asset identity roadmap

This is the decision record for removing magic-prefix level-series and asset
strings from Augur. The execution checklist lives in `../TODO.md`; keep this
file focused on the target shape and the invariants that make the staged cleanup
safe.

## Goal

Augur still has legacy identities like `security:btc`,
`home_value:san_francisco_ca`, `rent:vallejo_ca`, and
`private_equity:openai`. Those strings encode the kind in a prefix and require
parsing at boundaries. The endpoint is structural identity everywhere:

- Config and API payloads use typed unions with an explicit discriminator.
- Per-kind sampled frames carry only their natural sub-id column, such as
  `symbol`, `location_id`, or `issuer_id`.
- Trained artifacts store typed factor records instead of prefix-encoded names.
- `wire_id`, `parse_level_series_key`, `try_parse_level_series_key`,
  `parse_asset_key`, and `try_parse_asset_key` are deleted.

Bare identifiers are still fine when the column already supplies the kind. For
example, `issuer_id = "openai"` in a private-equity frame is not a magic prefix.

## Current State

The clean config surfaces are largely typed already:

- `LevelSeriesKind` and `AssetKind` are string enums.
- `LevelSeriesGroups[ValueT]` represents level-series config by structural
  roles.
- Independent provider config, independent series models, portfolio
  `value_series`, and `sample_sanity` checks accept typed keys.
- `SampledExogenousBundle` already has per-kind level fields plus a typed
  `PrivateEquityBundle`.

The remaining load-bearing prefix boundaries are intentionally narrow, but still
real:

- Sim compilation and decode still flatten level-series and asset identity into
  `series_id` / `asset_id` columns.
- State-space and VECM trained artifacts still serialize factor names as flat
  wire ids.
- Calibration catalogs and API/frontend wire still speak in flat ids for some
  cross-process surfaces.
- Downstream gaffer config must be migrated when a ducktape phase changes YAML
  shape. No compatibility shim is planned.

## Target Frame Shape

Each level-series kind owns its own frame. The frame name is the kind; rows carry
only the sub-id needed for that kind.

- Scalar levels: `inflation`; columns are rollout, month, value.
- Symbol levels: `security`; columns are rollout, month, symbol, value.
- Location levels: `home_value`, `rent`; columns are rollout, month,
  location_id, value.
- Private equity: `private_equity`; already its own bundle keyed by issuer.

This makes invalid combinations unrepresentable: an inflation row cannot have a
location id, and `home_value:san_francisco_ca` cannot collide with
`rent:san_francisco_ca`.

The simulator may keep dense numeric cubes internally. The cleanup target is how
those cubes are built and serialized, not the numeric storage layout.

## Staging

Each phase must land green on its own, with no permanent dual API.

1. **Config typed.** Most clean config surfaces have landed. The remaining
   config-like surfaces are tied to trained artifact factor names and move with
   Phase 3.
2. **Runtime frames typed.** Replace `series_id` / `asset_id` string dispatch in
   sampled frames, sim compiler inputs, codec helpers, projections, and decode.
3. **Artifacts typed.** Retype state-space JSON and VECM `trained_state` factor identity,
   regenerate artifacts, and then retype conditioning observations,
   `location_series_sources`, and `latest_observations`.
4. **Wire typed and guards added.** Retype API/frontend identity surfaces, delete
   parse/wire helpers, and add a CI guard against new prefix parsing.

## Downstream Sequencing

This monorepo does not carry backward compatibility for internal configs or
artifacts. The expected sequence for a breaking phase is:

1. Land the ducktape change green.
2. Repin the downstream repo.
3. Update downstream YAML/artifacts in the same downstream PR.
4. Let stale configs fail loudly instead of silently coercing old prefixes.

## Risks

- Artifact identity is the riskiest phase because trained blobs and conditioning
  observations must agree exactly.
- Calibration and API consumers may expose flat ids indirectly through catalog
  references; migrate those as explicit wire contracts rather than incidental
  parser cleanup.
- The final CI guard should be added only after the intended parser functions
  are gone, otherwise it will create noise instead of preventing regressions.
