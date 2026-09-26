# Strong entity identities

The desired endpoint is distinct strongly typed IDs at domain/API boundaries:
a security identity must not be usable where a property identity is required.
Changing string prefixes alone does not provide that guarantee.

The [roadmap](roadmap.md) tracks this as IDTYPES. Entity IDs are the nominal types
in `sim/ids.py` plus `model/series.py`'s `LocationId`, `IssuerId` and
`SecuritySymbol`. What remains:

- **The captured lot asset.** `SecurityLotState.asset_id`, `holdings.Disposition.asset_id`
  and the `lot_dispositions` frame column hold an `AssetKey` wire id
  (`security:SPY`) under the name the simulator uses for its `AssetId` (`SPY`).
  Either the capture carries the typed `AssetKey`, or the simulator's `AssetId`
  adopts the wire form; decide from the frame and product consumers. Either is a
  frame/wire change, made atomically with its readers.
- **Config-model construction.** `pydantic.mypy` runs without `init_typed`, so
  a non-strict model's `__init__` (the scenario and API config models) still
  accepts a bare `str` where an ID is declared; strict `Record`s and dataclasses
  are checked. Turning on `init_typed` is a repository-wide mypy change.

Labels, not entity identities, stay `str`: cause IDs, obligation IDs (a recurring
bill's cause-ID stem), consumption component IDs, `PreparedSeries.series_id` (the
wire form of an existing typed key), Plaid account IDs and catalog source IDs.

Decide boundary encoding from the concrete change, not a blanket prefix-renaming
campaign or a compatibility shim; no second supported format for an old deployment.
