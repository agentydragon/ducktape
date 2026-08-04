"""Shared API for exogenous path models consumed by the simulator.

Non-PE level series are grouped by **role** — the concern that
references them (see `augur/plans/typed_series_config.md`). A sampled bundle
carries one frame per `LevelSeriesKind` plus the PE bundle.

Which role a kind belongs to — asset-price (prices a lot), property-value
(values a property), index (escalates an amount) — is a row in `LEVEL_KIND_SPECS`,
not a shape in the storage. That table also carries each kind's frame schema, its
sub-id column, and how to rebuild a key from a sub-id, so the partition is stated
once and every helper below derives from it rather than restating it. Adding a kind
is a row; adding a role is a row plus a request channel.

Frames carry only a sub-id column (symbol / location_id) or nothing for a singleton
kind — never a magic-prefix `series_id` string. The sample/consume path is typed by
`LevelSeriesKey`, and the typed per-role request channels keep a lot from
being priced by inflation as a mypy error rather than a convention.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from numbers import Integral
from typing import Protocol, TypedDict, cast

import numpy as np
import polars as pl

from finance.augur.frames import concat_frames
from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import (
    AssetPriceKey,
    HomeValueKey,
    IndexSeriesKey,
    InflationKey,
    IssuerId,
    LevelSeriesKey,
    LevelSeriesKind,
    LocationId,
    PropertyValueKey,
    RentKey,
    SecurityKey,
    SecuritySymbol,
)

# Frame SHAPES. Several kinds share a shape (home_value and rent are both location-keyed);
# the shape is a property of the sub-id, not of the role.
SCALAR_LEVELS_SCHEMA = pl.Schema({"rollout_index": pl.Int64(), "month_index": pl.Int64(), "value": pl.Float64()})
SYMBOL_LEVELS_SCHEMA = pl.Schema(
    {"rollout_index": pl.Int64(), "month_index": pl.Int64(), "symbol": pl.Utf8(), "value": pl.Float64()}
)
LOCATION_LEVELS_SCHEMA = pl.Schema(
    {"rollout_index": pl.Int64(), "month_index": pl.Int64(), "location_id": pl.Utf8(), "value": pl.Float64()}
)


class SeriesRole(StrEnum):
    """What a level series is referenced BY — the partition `LevelSeriesKey` is split along.

    Values are the request-channel/bundle field stems (`required_asset_prices`,
    `asset_prices`), so the wire vocabulary follows from the enum rather than being
    repeated next to it.
    """

    ASSET_PRICES = "asset_prices"
    PROPERTY_VALUES = "property_values"
    INDEX_SERIES = "index_series"


@dataclass(frozen=True)
class LevelKindSpec:
    """Everything that varies per level-series kind, in one place.

    This table is the whole reason the role partition is not restated in every
    helper below. Adding a kind is a row here; adding a role is a row here plus a
    request channel on `ExogenousSamplingRequest`. Nothing else fans out by hand.
    """

    role: SeriesRole
    schema: pl.Schema
    subid_column: str | None
    key_for_subid: Callable[[str], LevelSeriesKey]


LEVEL_KIND_SPECS: Mapping[LevelSeriesKind, LevelKindSpec] = {
    LevelSeriesKind.SECURITY: LevelKindSpec(
        SeriesRole.ASSET_PRICES, SYMBOL_LEVELS_SCHEMA, "symbol", lambda s: SecurityKey(symbol=SecuritySymbol(s))
    ),
    LevelSeriesKind.HOME_VALUE: LevelKindSpec(
        SeriesRole.PROPERTY_VALUES,
        LOCATION_LEVELS_SCHEMA,
        "location_id",
        lambda s: HomeValueKey(location_id=LocationId(s)),
    ),
    LevelSeriesKind.INFLATION: LevelKindSpec(
        SeriesRole.INDEX_SERIES, SCALAR_LEVELS_SCHEMA, None, lambda _: InflationKey()
    ),
    LevelSeriesKind.RENT: LevelKindSpec(
        SeriesRole.INDEX_SERIES, LOCATION_LEVELS_SCHEMA, "location_id", lambda s: RentKey(location_id=LocationId(s))
    ),
}


def series_role(key: LevelSeriesKey) -> SeriesRole:
    return LEVEL_KIND_SPECS[key.kind].role


@dataclass(frozen=True)
class LevelFrames:
    """Sampled level frames, one per `LevelSeriesKind`, flat.

    Flat by kind rather than nested by role: the role is a property of the
    kind (`LEVEL_KIND_SPECS`), so nesting it in the storage shape would force every
    producer, merger and consumer to restate the partition. Callers that want one
    role ask for it (`by_role`); callers that want one series ask by kind.
    """

    by_kind: Mapping[LevelSeriesKind, pl.DataFrame]

    def __post_init__(self) -> None:
        missing = set(LevelSeriesKind) - set(self.by_kind)
        if missing:
            raise ValueError(f"LevelFrames missing kinds {sorted(missing)}")
        for kind, frame in self.by_kind.items():
            _require_schema(frame, LEVEL_KIND_SPECS[kind].schema, frame_name=str(kind))

    @classmethod
    def empty(cls) -> LevelFrames:
        return cls(by_kind={kind: spec.schema.to_frame() for kind, spec in LEVEL_KIND_SPECS.items()})

    @classmethod
    def from_partial(cls, frames: Mapping[LevelSeriesKind, pl.DataFrame]) -> LevelFrames:
        """Build from the kinds a caller actually produced; the rest default to empty."""

        return cls(by_kind={kind: frames.get(kind, spec.schema.to_frame()) for kind, spec in LEVEL_KIND_SPECS.items()})

    def frame(self, kind: LevelSeriesKind) -> pl.DataFrame:
        return self.by_kind[kind]

    def by_role(self, role: SeriesRole) -> dict[LevelSeriesKind, pl.DataFrame]:
        return {kind: frame for kind, frame in self.by_kind.items() if LEVEL_KIND_SPECS[kind].role is role}

    def value_rows(self) -> list[tuple[LevelSeriesKey, pl.DataFrame]]:
        """Every distinct series as `(key, (rollout_index, month_index, value) frame)`.

        Ordered by `wire_id` so a consumer that assigns row indices from this order (the sim's
        compiled cube) gets the same assignment for the same content — polars `unique` alone
        returns hash order, which would re-trace the jitted program on every other compile.
        """

        rows: list[tuple[LevelSeriesKey, pl.DataFrame]] = []
        for kind, spec in LEVEL_KIND_SPECS.items():
            frame = self.frame(kind)
            if frame.is_empty():
                continue
            if spec.subid_column is None:
                rows.append((spec.key_for_subid(""), frame))
                continue
            subid_column = spec.subid_column
            rows.extend(
                (
                    spec.key_for_subid(subid),
                    frame.filter(pl.col(subid_column) == subid).select("rollout_index", "month_index", "value"),
                )
                for subid in sorted(_string_values(frame, subid_column))
            )
        return sorted(rows, key=lambda row: row[0].wire_id)

    def series_keys(self) -> frozenset[LevelSeriesKey]:
        """The distinct typed keys present across all roles."""

        return frozenset(key for key, _ in self.value_rows())


@dataclass(frozen=True)
class ExogenousSamplingRequest:
    """Request metadata passed to an exogenous path model sample.

    Required non-PE level series are split by role so a consumer states
    exactly which kind of series it needs: `required_asset_prices` (price a
    lot), `required_property_values` (value a property), `required_index_series`
    (escalate an amount). PE issuers (carrying the whole `PrivateEquityBundle`
    per issuer) are required by `required_private_equity_issuers`; PE tender
    events and protocol channels are part of the PE bundle, not separate
    channels. `required_level_series` unions the level roles for the
    provider/validate code that ranges over all non-PE level series uniformly.
    """

    horizon_months: int
    rollout_seeds: tuple[int, ...]
    required_asset_prices: frozenset[AssetPriceKey] = frozenset()
    required_property_values: frozenset[PropertyValueKey] = frozenset()
    required_index_series: frozenset[IndexSeriesKey] = frozenset()
    required_private_equity_issuers: frozenset[IssuerId] = frozenset()

    def __post_init__(self) -> None:
        if self.horizon_months < 0:
            raise ValueError("horizon_months must be non-negative")
        seeds = tuple(self.rollout_seeds)
        if not all(isinstance(seed, Integral) for seed in seeds):
            raise TypeError("rollout_seeds must contain integers")
        seeds = tuple(int(seed) for seed in seeds)
        if any(seed < 0 for seed in seeds):
            raise ValueError("rollout_seeds must be non-negative")
        object.__setattr__(self, "rollout_seeds", seeds)

    @property
    def rollout_count(self) -> int:
        """Number of paths requested, derived from the explicit seed vector."""

        return len(self.rollout_seeds)

    @property
    def required_level_series(self) -> frozenset[LevelSeriesKey]:
        """All required non-PE level series, unioned across the roles."""

        return frozenset(self.required_asset_prices | self.required_property_values | self.required_index_series)


@dataclass(frozen=True)
class SampledExogenousBundle:
    """Polars-native joint sample of exogenous levels and PE protocol.

    `levels` holds one frame per kind (see `LevelFrames`); `private_equity` carries the
    typed PE protocol bundle per issuer.
    """

    levels: LevelFrames = field(default_factory=LevelFrames.empty)
    private_equity: PrivateEquityBundle = field(default_factory=PrivateEquityBundle.empty)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def level_matrix(self, key: LevelSeriesKey, *, rollout_count: int, horizon_months: int) -> np.ndarray:
        """Return one level series as a `(rollout, month)` matrix."""

        spec = LEVEL_KIND_SPECS[key.kind]
        frame = self.levels.frame(key.kind)
        if spec.subid_column is not None:
            if key.subid is None:
                raise ValueError(f"{key.wire_id!r} is keyed by {spec.subid_column} but has subid=None")
            frame = frame.filter(pl.col(spec.subid_column) == key.subid)
        return _matrix_from_long_frame(
            frame,
            value_column="value",
            rollout_count=rollout_count,
            horizon_months=horizon_months,
            dtype=np.float64,
            label=str(key.wire_id),
        )


class LevelRequestChannels(TypedDict):
    """The role request channels of `ExogenousSamplingRequest`.

    Typed per role (not one flat set) so a consumer cannot ask for a rent series
    where a lot price is expected. That type safety is the one thing worth restating the
    partition for; everything mechanical derives from `LEVEL_KIND_SPECS`.
    """

    required_asset_prices: frozenset[AssetPriceKey]
    required_property_values: frozenset[PropertyValueKey]
    required_index_series: frozenset[IndexSeriesKey]


def level_series_request_channels(keys: Iterable[LevelSeriesKey]) -> LevelRequestChannels:
    """Partition a mixed set of level keys into the role request channels.

    For callers that hold a `LevelSeriesKey` set (e.g. a sanity spec listing required
    series across roles) and want to splat it into the request:
    `ExogenousSamplingRequest(..., **level_series_request_channels(keys))`.
    """

    by_role: dict[SeriesRole, set[LevelSeriesKey]] = {m: set() for m in SeriesRole}
    for key in keys:
        by_role[series_role(key)].add(key)
    # The casts are the one place the runtime partition meets the static one; every key
    # routed here came from `LEVEL_KIND_SPECS`, so membership is exactly the union.
    return {
        "required_asset_prices": cast("frozenset[AssetPriceKey]", frozenset(by_role[SeriesRole.ASSET_PRICES])),
        "required_property_values": cast("frozenset[PropertyValueKey]", frozenset(by_role[SeriesRole.PROPERTY_VALUES])),
        "required_index_series": cast("frozenset[IndexSeriesKey]", frozenset(by_role[SeriesRole.INDEX_SERIES])),
    }


class Sampler(Protocol):
    """Runtime sampling boundary — required of every augur exogenous model.

    Anything that can't be sampled is unusable in the augur sim. `Fittable`
    (offline trainer) and `Scorable` (metric battery) extend this protocol
    for models that additionally support training / scoring.

    `emittable_level_keys` / `emittable_private_equity_issuers` advertise what the
    sampler is configured to produce. Consumers (sample-sanity / calibration) partition
    a list of desired series into modeled-vs-unmodeled by intersecting with these sets,
    so a check against a series the deployment's model doesn't cover renders as
    `unmodeled` instead of hard-failing the sample request.
    """

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        """Return all modeled external drivers as a sampled levels bundle."""
        ...

    def emittable_level_keys(self) -> frozenset[LevelSeriesKey]:
        """Level-series keys this sampler is configured to produce in a sampled bundle."""
        ...

    def emittable_private_equity_issuers(self) -> frozenset[IssuerId]:
        """PE issuers this sampler is configured to produce in `sampled.private_equity`."""
        ...


def _level_row_frame(
    key: LevelSeriesKey, levels: np.ndarray, *, rollout_count: int, horizon_months: int
) -> pl.DataFrame:
    """Build the single-series long frame for `key` in its kind's schema."""

    expected_shape = (rollout_count, horizon_months + 1)
    if levels.shape != expected_shape:
        raise ValueError(f"series {key.wire_id!r} produced levels with shape {levels.shape}; expected {expected_shape}")
    rollout_idx, month_idx = _long_indices(rollout_count=rollout_count, horizon_months=horizon_months)
    spec = LEVEL_KIND_SPECS[key.kind]
    columns: dict[str, object] = {"rollout_index": rollout_idx, "month_index": month_idx}
    if spec.subid_column is not None:
        columns[spec.subid_column] = [key.subid] * (rollout_count * (horizon_months + 1))
    columns["value"] = levels.reshape(-1)
    return pl.DataFrame(columns, schema=spec.schema)


def assemble_level_frames(
    blocks: Iterable[tuple[LevelSeriesKey, np.ndarray]], *, rollout_count: int, horizon_months: int
) -> LevelFrames:
    """Assemble sampled `(key, matrix)` blocks into per-kind frames.

    One flat iterable, because each key already knows its kind and each kind already
    knows its role. Producers used to hand a separate list per role and
    every one of them had to remember all of them — which is exactly how a channel gets
    silently dropped.
    """

    rows_by_kind: dict[LevelSeriesKind, list[pl.DataFrame]] = {kind: [] for kind in LEVEL_KIND_SPECS}
    for key, matrix in blocks:
        rows_by_kind[key.kind].append(
            _level_row_frame(key, matrix, rollout_count=rollout_count, horizon_months=horizon_months)
        )
    return LevelFrames(
        by_kind={kind: concat_frames(rows, LEVEL_KIND_SPECS[kind].schema) for kind, rows in rows_by_kind.items()}
    )


def merge_level_frames(left: LevelFrames, right: LevelFrames) -> LevelFrames:
    """Per-kind concat of two bundles' level frames, rejecting duplicate sub-ids / singleton collisions."""

    def merge(kind: LevelSeriesKind) -> pl.DataFrame:
        spec = LEVEL_KIND_SPECS[kind]
        left_frame = left.frame(kind)
        right_frame = right.frame(kind)
        if spec.subid_column is None:
            if not left_frame.is_empty() and not right_frame.is_empty():
                raise ValueError(f"composite exogenous providers both produced the singleton {kind} series")
        else:
            _reject_duplicate_subids(left_frame, right_frame, subid_column=spec.subid_column, label=f"{kind} series")
        return concat_frames([left_frame, right_frame], spec.schema)

    return LevelFrames(by_kind={kind: merge(kind) for kind in LEVEL_KIND_SPECS})


def _reject_duplicate_subids(left: pl.DataFrame, right: pl.DataFrame, *, subid_column: str, label: str) -> None:
    duplicate = sorted(_string_values(left, subid_column) & _string_values(right, subid_column))
    if duplicate:
        raise ValueError(f"composite exogenous providers produced duplicate {label}: {duplicate}")


def validate_sample_satisfies_request(request: ExogenousSamplingRequest, sampled: SampledExogenousBundle) -> None:
    """Validate that a sampled bundle covers the consumer-requested keys.

    Providers are free to sample extra series. The request's required keys
    are a consumer compatibility contract, enforced at the boundary that
    consumes the provider.
    """

    missing_level_series = sorted(
        (key for key in request.required_level_series if not _bundle_has_key(sampled, key)), key=lambda key: key.wire_id
    )
    sampled_pe_issuers = frozenset(IssuerId(str(issuer)) for issuer in sampled.private_equity.issuer_ids())
    missing_pe_issuers = sorted(request.required_private_equity_issuers - sampled_pe_issuers)
    if not missing_level_series and not missing_pe_issuers:
        return

    details: list[str] = []
    if missing_level_series:
        details.append(f"missing required level series: {[key.wire_id for key in missing_level_series]}")
    if missing_pe_issuers:
        details.append(f"missing required private-equity issuer(s): {missing_pe_issuers}")
    raise ValueError("sampled exogenous bundle " + "; ".join(details))


def _bundle_has_key(sampled: SampledExogenousBundle, key: LevelSeriesKey) -> bool:
    spec = LEVEL_KIND_SPECS[key.kind]
    frame = sampled.levels.frame(key.kind)
    if frame.is_empty():
        return False
    if spec.subid_column is None:
        return True
    return str(key.subid) in _string_values(frame, spec.subid_column)


_EMPTY_LEVEL_ANCHORS: Mapping[LevelSeriesKey, float] = {}
_EMPTY_PE_ANCHORS: Mapping[IssuerId, float] = {}


def anchor_sampled_series_levels(
    sampled: SampledExogenousBundle,
    *,
    level_series_anchors: Mapping[LevelSeriesKey, float] = _EMPTY_LEVEL_ANCHORS,
    private_equity_anchors: Mapping[IssuerId, float] = _EMPTY_PE_ANCHORS,
) -> SampledExogenousBundle:
    """Rescale sampled paths so month-0 values match the supplied anchors.

    `level_series_anchors` keys non-PE levels by `LevelSeriesKey`.
    `private_equity_anchors` keys the PE bundle's per-unit mark by `IssuerId`.
    Each series is rescaled per-rollout: its month-0 value for a rollout sets
    that rollout's base.
    """

    level_anchors_typed = dict(level_series_anchors)
    pe_anchors_typed = {IssuerId(str(issuer)): float(value) for issuer, value in dict(private_equity_anchors).items()}
    metadata_extras: dict[str, object] = {}
    if level_anchors_typed:
        metadata_extras["level_anchors"] = {key.wire_id: float(value) for key, value in level_anchors_typed.items()}
    if pe_anchors_typed:
        metadata_extras["private_equity_anchors"] = pe_anchors_typed

    private_equity = _anchor_private_equity_marks(sampled.private_equity, pe_anchors_typed)

    # Partition anchors by kind -> {sub-id-or-None: target month-0 value}.
    anchors_by_kind: dict[LevelSeriesKind, dict[str | None, float]] = {kind: {} for kind in LEVEL_KIND_SPECS}
    for key, value in level_anchors_typed.items():
        anchors_by_kind[key.kind][key.subid] = float(value)

    return SampledExogenousBundle(
        levels=LevelFrames(
            by_kind={
                kind: _anchor_level_frame(sampled.levels.frame(kind), kind, anchors_by_kind[kind])
                for kind in LEVEL_KIND_SPECS
            }
        ),
        private_equity=private_equity,
        metadata={**sampled.metadata, **metadata_extras},
    )


def _anchor_level_frame(
    frame: pl.DataFrame, kind: LevelSeriesKind, anchors_for_kind: Mapping[str | None, float]
) -> pl.DataFrame:
    """Rescale one per-kind frame so each series' per-rollout month-0 value matches its anchor."""

    if frame.is_empty() or not anchors_for_kind:
        return frame
    schema = LEVEL_KIND_SPECS[kind].schema
    subid_column = LEVEL_KIND_SPECS[kind].subid_column

    if subid_column is None:
        anchor_value = next(iter(anchors_for_kind.values()))
        bases = frame.filter(pl.col("month_index") == 0).select("rollout_index", pl.col("value").alias("_base_value"))
        if not bases.filter(pl.col("_base_value") == 0.0).is_empty():
            raise ValueError(f"sampled series {kind!r} has zero month-0 value and cannot be anchored")
        return (
            frame.join(bases, on="rollout_index", how="left")
            .with_columns(value=pl.col("value") * anchor_value / pl.col("_base_value"))
            .select(schema.names())
        )

    active = {sub: val for sub, val in anchors_for_kind.items() if sub is not None}
    if not active:
        return frame
    anchor_frame = pl.DataFrame(
        {subid_column: list(active), "_anchor_value": list(active.values())},
        schema={subid_column: pl.Utf8(), "_anchor_value": pl.Float64()},
    )
    bases = (
        frame.filter(pl.col("month_index") == 0)
        .join(anchor_frame, on=subid_column, how="inner")
        .select("rollout_index", subid_column, "_anchor_value", pl.col("value").alias("_base_value"))
    )
    zero_bases = bases.filter(pl.col("_base_value") == 0.0)
    if not zero_bases.is_empty():
        bad = sorted(set(zero_bases.get_column(subid_column).to_list()))
        raise ValueError(f"sampled {kind} series have zero month-0 value and cannot be anchored: {bad}")
    return (
        frame.join(bases, on=["rollout_index", subid_column], how="left")
        .with_columns(
            value=pl.when(pl.col("_anchor_value").is_not_null())
            .then(pl.col("value") * pl.col("_anchor_value") / pl.col("_base_value"))
            .otherwise(pl.col("value"))
        )
        .select(schema.names())
    )


def _anchor_private_equity_marks(pe: PrivateEquityBundle, anchors: Mapping[IssuerId, float]) -> PrivateEquityBundle:
    if pe.is_empty() or not anchors:
        return pe
    issuer_anchor = {str(issuer): float(value) for issuer, value in anchors.items()}
    frame = pe.frame
    base_frame = frame.filter(pl.col("month_index") == 0).select(
        "rollout_index", "issuer_id", pl.col("mark_usd_per_unit").alias("_base_value")
    )
    anchor_frame = pl.DataFrame(
        {"issuer_id": list(issuer_anchor), "_anchor_value": list(issuer_anchor.values())},
        schema={"issuer_id": pl.Utf8(), "_anchor_value": pl.Float64()},
    )
    joined = (
        frame.join(base_frame, on=["rollout_index", "issuer_id"], how="left")
        .join(anchor_frame, on="issuer_id", how="left")
        .with_columns(
            mark_usd_per_unit=pl.when(pl.col("_anchor_value").is_not_null() & (pl.col("_base_value") > 0.0))
            .then(pl.col("mark_usd_per_unit") * pl.col("_anchor_value") / pl.col("_base_value"))
            .otherwise(pl.col("mark_usd_per_unit"))
        )
        .select(frame.columns)
    )
    return PrivateEquityBundle(frame=joined)


def _long_indices(*, rollout_count: int, horizon_months: int) -> tuple[np.ndarray, np.ndarray]:
    return (
        np.repeat(np.arange(rollout_count, dtype=np.int64), horizon_months + 1),
        np.tile(np.arange(horizon_months + 1, dtype=np.int64), rollout_count),
    )


def _require_schema(frame: pl.DataFrame, expected: pl.Schema, *, frame_name: str) -> None:
    if frame.schema != expected:
        raise ValueError(f"{frame_name} schema must be {expected}, got {frame.schema}")


def _string_values(frame: pl.DataFrame, column: str) -> frozenset[str]:
    if frame.is_empty():
        return frozenset()
    return frozenset(str(value) for value in frame.get_column(column).unique().to_list())


def _matrix_from_long_frame(
    frame: pl.DataFrame,
    *,
    value_column: str,
    rollout_count: int,
    horizon_months: int,
    dtype: type[np.generic],
    label: str,
) -> np.ndarray:
    selected = frame.sort(["rollout_index", "month_index"])
    if selected.is_empty():
        raise KeyError(f"missing sampled series {label!r}")

    expected_rows = rollout_count * (horizon_months + 1)
    if selected.height != expected_rows:
        raise ValueError(f"sampled series {label!r} has {selected.height} rows; expected {expected_rows}")

    expected_rollouts, expected_months = _long_indices(rollout_count=rollout_count, horizon_months=horizon_months)
    actual_rollouts = selected.get_column("rollout_index").to_numpy()
    actual_months = selected.get_column("month_index").to_numpy()
    if not np.array_equal(actual_rollouts, expected_rollouts) or not np.array_equal(actual_months, expected_months):
        raise ValueError(f"sampled series {label!r} does not cover every rollout/month exactly once")

    return selected.get_column(value_column).to_numpy().astype(dtype).reshape((rollout_count, horizon_months + 1))
