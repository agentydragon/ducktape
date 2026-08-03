"""Shared API for exogenous path models consumed by the simulator.

Non-PE level series are grouped by **magisterium** — the concern that
references them (see `augur/plans/typed_series_config.md`). A sampled bundle
carries four level magisteria plus the PE bundle:

- `asset_prices` — `sp500` (scalar) + `crypto` (symbol-keyed); price a lot.
- `property_values` — `home_value` (location-keyed); value a property.
- `index_series` — `inflation` (scalar) + `rent` (location-keyed); escalate an amount.
- `discount_rates` — `nominal_yield` + `muni_ratio` (both tenor-keyed); discount and
  mark a fixed-income instrument.

Each magisterium's frame carries only a sub-id column (symbol / location_id /
tenor_months) or nothing for a singleton — never a magic-prefix `series_id` string. The model's
sample/consume path is typed by `LevelSeriesKey` (the magisterium sum), which
routes internally to the right frame.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from numbers import Integral
from typing import Protocol, TypedDict

import numpy as np
import polars as pl

from finance.augur.frames import concat_frames
from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import (
    AssetPriceKey,
    CryptoKey,
    CryptoSymbol,
    DiscountRateKey,
    HomeValueKey,
    IndexSeriesKey,
    InflationKey,
    IssuerId,
    LevelSeriesKey,
    LevelSeriesKind,
    LocationId,
    MuniRatioKey,
    NominalYieldKey,
    PropertyValueKey,
    RentKey,
    SP500Key,
    TenorMonths,
)

# Four frame SHAPES (the field name carries the kind; home_value and rent share the LOCATION
# shape, nominal_yield and muni_ratio share the TENOR shape, but each is a distinct frame in
# its own magisterium).
SCALAR_LEVELS_SCHEMA = pl.Schema({"rollout_index": pl.Int64(), "month_index": pl.Int64(), "value": pl.Float64()})
SYMBOL_LEVELS_SCHEMA = pl.Schema(
    {"rollout_index": pl.Int64(), "month_index": pl.Int64(), "symbol": pl.Utf8(), "value": pl.Float64()}
)
LOCATION_LEVELS_SCHEMA = pl.Schema(
    {"rollout_index": pl.Int64(), "month_index": pl.Int64(), "location_id": pl.Utf8(), "value": pl.Float64()}
)
TENOR_LEVELS_SCHEMA = pl.Schema(
    {"rollout_index": pl.Int64(), "month_index": pl.Int64(), "tenor_months": pl.Int64(), "value": pl.Float64()}
)

# Per-kind frame metadata, keyed by the StrEnum kind (whose value equals the
# bundle field name). `subid_column` is None for singletons.
_SCHEMA_BY_KIND: dict[LevelSeriesKind, pl.Schema] = {
    LevelSeriesKind.INFLATION: SCALAR_LEVELS_SCHEMA,
    LevelSeriesKind.SP500: SCALAR_LEVELS_SCHEMA,
    LevelSeriesKind.CRYPTO: SYMBOL_LEVELS_SCHEMA,
    LevelSeriesKind.HOME_VALUE: LOCATION_LEVELS_SCHEMA,
    LevelSeriesKind.RENT: LOCATION_LEVELS_SCHEMA,
    LevelSeriesKind.NOMINAL_YIELD: TENOR_LEVELS_SCHEMA,
    LevelSeriesKind.MUNI_RATIO: TENOR_LEVELS_SCHEMA,
}
_SUBID_COLUMN_BY_KIND: dict[LevelSeriesKind, str | None] = {
    LevelSeriesKind.INFLATION: None,
    LevelSeriesKind.SP500: None,
    LevelSeriesKind.CRYPTO: "symbol",
    LevelSeriesKind.HOME_VALUE: "location_id",
    LevelSeriesKind.RENT: "location_id",
    LevelSeriesKind.NOMINAL_YIELD: "tenor_months",
    LevelSeriesKind.MUNI_RATIO: "tenor_months",
}

# Rates are the one magisterium anchored ADDITIVELY. Every other level series is a positive
# level whose month-0 value scales the path; a yield may legitimately sit at or near zero, so
# scaling is both undefined and wrong -- shifting the whole curve onto today's observed level
# is what a rate anchor means.
_ADDITIVELY_ANCHORED_KINDS: frozenset[LevelSeriesKind] = frozenset(
    {LevelSeriesKind.NOMINAL_YIELD, LevelSeriesKind.MUNI_RATIO}
)


def _key_subid(key: LevelSeriesKey) -> str | int:
    """The key's sub-id in its frame column's OWN dtype.

    A tenor column is `Int64`, not `Utf8`, so a stringified tenor would neither build a
    frame nor match a `pl.col(...) == ...` filter. Callers comparing against stringified
    column values must `str()` this themselves.
    """

    match key:
        case CryptoKey(symbol=symbol):
            return str(symbol)
        case HomeValueKey(location_id=location_id) | RentKey(location_id=location_id):
            return str(location_id)
        case NominalYieldKey(tenor_months=tenor) | MuniRatioKey(tenor_months=tenor):
            return int(tenor)
        case InflationKey() | SP500Key():
            raise ValueError(f"{key.kind} is a singleton level series and has no sub-id")


@dataclass(frozen=True)
class ExogenousSamplingRequest:
    """Request metadata passed to an exogenous path model sample.

    Required non-PE level series are split by magisterium so a consumer states
    exactly which kind of series it needs: `required_asset_prices` (price a
    lot), `required_property_values` (value a property), `required_index_series`
    (escalate an amount). PE issuers (carrying the whole `PrivateEquityBundle`
    per issuer) are required by `required_private_equity_issuers`; PE tender
    events and protocol channels are part of the PE bundle, not separate
    channels. `required_level_series` unions the four level magisteria for the
    provider/validate code that ranges over all non-PE level series uniformly.
    """

    horizon_months: int
    rollout_seeds: tuple[int, ...]
    required_asset_prices: frozenset[AssetPriceKey] = frozenset()
    required_property_values: frozenset[PropertyValueKey] = frozenset()
    required_index_series: frozenset[IndexSeriesKey] = frozenset()
    required_discount_rates: frozenset[DiscountRateKey] = frozenset()
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
        """All required non-PE level series, unioned across the four magisteria."""

        return frozenset(
            self.required_asset_prices
            | self.required_property_values
            | self.required_index_series
            | self.required_discount_rates
        )


@dataclass(frozen=True)
class AssetPriceFrames:
    """Asset-price magisterium: per-unit price paths that value a holding/lot."""

    sp500: pl.DataFrame = field(default_factory=SCALAR_LEVELS_SCHEMA.to_frame)
    crypto: pl.DataFrame = field(default_factory=SYMBOL_LEVELS_SCHEMA.to_frame)


@dataclass(frozen=True)
class IndexSeriesFrames:
    """Index magisterium: level paths that escalate a recurring amount."""

    inflation: pl.DataFrame = field(default_factory=SCALAR_LEVELS_SCHEMA.to_frame)
    rent: pl.DataFrame = field(default_factory=LOCATION_LEVELS_SCHEMA.to_frame)


@dataclass(frozen=True)
class DiscountRateFrames:
    """Discount-rate magisterium: the nominal par curve and the muni ratio off it, by tenor."""

    nominal_yield: pl.DataFrame = field(default_factory=TENOR_LEVELS_SCHEMA.to_frame)
    muni_ratio: pl.DataFrame = field(default_factory=TENOR_LEVELS_SCHEMA.to_frame)


@dataclass(frozen=True)
class SampledExogenousBundle:
    """Polars-native joint sample of exogenous levels and PE protocol.

    Level series are grouped by magisterium; `property_values` is the single
    `home_value` frame (location-keyed). Each frame's identity is its kind, so
    rows carry only a sub-id column (symbol / location_id) or nothing for a
    singleton. `private_equity` carries the typed PE protocol bundle per issuer.
    """

    asset_prices: AssetPriceFrames = field(default_factory=AssetPriceFrames)
    property_values: pl.DataFrame = field(default_factory=LOCATION_LEVELS_SCHEMA.to_frame)
    index_series: IndexSeriesFrames = field(default_factory=IndexSeriesFrames)
    discount_rates: DiscountRateFrames = field(default_factory=DiscountRateFrames)
    private_equity: PrivateEquityBundle = field(default_factory=PrivateEquityBundle.empty)
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for kind in LevelSeriesKind:
            _require_schema(self._frame_for_kind(kind), _SCHEMA_BY_KIND[kind], frame_name=str(kind))

    def _frame_for_kind(self, kind: LevelSeriesKind) -> pl.DataFrame:
        match kind:
            case LevelSeriesKind.SP500:
                return self.asset_prices.sp500
            case LevelSeriesKind.CRYPTO:
                return self.asset_prices.crypto
            case LevelSeriesKind.HOME_VALUE:
                return self.property_values
            case LevelSeriesKind.INFLATION:
                return self.index_series.inflation
            case LevelSeriesKind.RENT:
                return self.index_series.rent
            case LevelSeriesKind.NOMINAL_YIELD:
                return self.discount_rates.nominal_yield
            case LevelSeriesKind.MUNI_RATIO:
                return self.discount_rates.muni_ratio

    def level_matrix(self, key: LevelSeriesKey, *, rollout_count: int, horizon_months: int) -> np.ndarray:
        """Return one level series as a `(rollout, month)` matrix."""

        frame = self._frame_for_kind(key.kind)
        subid_column = _SUBID_COLUMN_BY_KIND[key.kind]
        if subid_column is not None:
            frame = frame.filter(pl.col(subid_column) == _key_subid(key))
        return _matrix_from_long_frame(
            frame,
            value_column="value",
            rollout_count=rollout_count,
            horizon_months=horizon_months,
            dtype=np.float64,
            label=str(key.wire_id),
        )


class LevelRequestChannels(TypedDict):
    """The four magisterium request channels of `ExogenousSamplingRequest`."""

    required_asset_prices: frozenset[AssetPriceKey]
    required_property_values: frozenset[PropertyValueKey]
    required_index_series: frozenset[IndexSeriesKey]
    required_discount_rates: frozenset[DiscountRateKey]


def level_series_request_channels(keys: Iterable[LevelSeriesKey]) -> LevelRequestChannels:
    """Partition a mixed set of level keys into the magisterium request channels.

    For callers that hold a `LevelSeriesKey` set (e.g. a sanity spec listing
    required series across magisteria) and want to splat it into the request:
    `ExogenousSamplingRequest(..., **level_series_request_channels(keys))`.
    """

    asset_prices: set[AssetPriceKey] = set()
    property_values: set[PropertyValueKey] = set()
    index_series: set[IndexSeriesKey] = set()
    discount_rates: set[DiscountRateKey] = set()
    for key in keys:
        match key:
            case SP500Key() | CryptoKey():
                asset_prices.add(key)
            case HomeValueKey():
                property_values.add(key)
            case InflationKey() | RentKey():
                index_series.add(key)
            case NominalYieldKey() | MuniRatioKey():
                discount_rates.add(key)
    return {
        "required_asset_prices": frozenset(asset_prices),
        "required_property_values": frozenset(property_values),
        "required_index_series": frozenset(index_series),
        "required_discount_rates": frozenset(discount_rates),
    }


def partition_level_blocks(
    blocks: Iterable[tuple[LevelSeriesKey, np.ndarray]],
) -> tuple[
    list[tuple[AssetPriceKey, np.ndarray]],
    list[tuple[PropertyValueKey, np.ndarray]],
    list[tuple[IndexSeriesKey, np.ndarray]],
    list[tuple[DiscountRateKey, np.ndarray]],
]:
    """Partition flat `(LevelSeriesKey, matrix)` blocks into the four magisterium groups.

    The typed fan-out sibling of `level_series_request_channels` (which partitions bare
    keys). For callers whose level identity is still flat — the trained models keyed by a
    flat factor tuple — this routes each sampled block to its magisterium so the four lists
    can be splatted into `assemble_level_magisteria`. The primary per-series providers never
    need it: they hold their specs magisterium-separated from the start.
    """

    asset_price_blocks: list[tuple[AssetPriceKey, np.ndarray]] = []
    property_value_blocks: list[tuple[PropertyValueKey, np.ndarray]] = []
    index_blocks: list[tuple[IndexSeriesKey, np.ndarray]] = []
    discount_rate_blocks: list[tuple[DiscountRateKey, np.ndarray]] = []
    for key, matrix in blocks:
        match key:
            case SP500Key() | CryptoKey():
                asset_price_blocks.append((key, matrix))
            case HomeValueKey():
                property_value_blocks.append((key, matrix))
            case InflationKey() | RentKey():
                index_blocks.append((key, matrix))
            case NominalYieldKey() | MuniRatioKey():
                discount_rate_blocks.append((key, matrix))
    return asset_price_blocks, property_value_blocks, index_blocks, discount_rate_blocks


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
    columns: dict[str, object] = {"rollout_index": rollout_idx, "month_index": month_idx}
    subid_column = _SUBID_COLUMN_BY_KIND[key.kind]
    if subid_column is not None:
        columns[subid_column] = [_key_subid(key)] * (rollout_count * (horizon_months + 1))
    columns["value"] = levels.reshape(-1)
    return pl.DataFrame(columns, schema=_SCHEMA_BY_KIND[key.kind])


class LevelBundleKwargs(TypedDict):
    """The level-magisterium fields of `SampledExogenousBundle`, splattable into it.

    A `TypedDict` (not `dict[str, object]`) so `SampledExogenousBundle(**kwargs,
    private_equity=…, metadata=…)` typechecks at every producer/merger call site.
    """

    asset_prices: AssetPriceFrames
    property_values: pl.DataFrame
    index_series: IndexSeriesFrames
    discount_rates: DiscountRateFrames


@dataclass(frozen=True)
class LevelMagisteria:
    """The four level magisteria, assembled and ready to splat into a bundle."""

    asset_prices: AssetPriceFrames
    property_values: pl.DataFrame
    index_series: IndexSeriesFrames
    discount_rates: DiscountRateFrames

    def as_bundle_kwargs(self) -> LevelBundleKwargs:
        return {
            "asset_prices": self.asset_prices,
            "property_values": self.property_values,
            "index_series": self.index_series,
            "discount_rates": self.discount_rates,
        }


def assemble_level_magisteria(
    *,
    asset_price_blocks: Iterable[tuple[AssetPriceKey, np.ndarray]],
    property_value_blocks: Iterable[tuple[PropertyValueKey, np.ndarray]],
    index_blocks: Iterable[tuple[IndexSeriesKey, np.ndarray]],
    discount_rate_blocks: Iterable[tuple[DiscountRateKey, np.ndarray]] = (),
    rollout_count: int,
    horizon_months: int,
) -> LevelMagisteria:
    """Assemble sampled `(key, matrix)` blocks into the four magisterium frame groups.

    Blocks arrive already separated by magisterium — there is no cross-magisterium
    bucket to route. Within a magisterium the singleton-vs-keyed split (sp500 vs
    crypto, inflation vs rent) is a local `isinstance` on that magisterium's own key
    union; the property-value magisterium has the single `home_value` kind.
    """

    def row(key: LevelSeriesKey, matrix: np.ndarray) -> pl.DataFrame:
        return _level_row_frame(key, matrix, rollout_count=rollout_count, horizon_months=horizon_months)

    sp500_rows: list[pl.DataFrame] = []
    crypto_rows: list[pl.DataFrame] = []
    for asset_key, asset_matrix in asset_price_blocks:
        (sp500_rows if isinstance(asset_key, SP500Key) else crypto_rows).append(row(asset_key, asset_matrix))

    inflation_rows: list[pl.DataFrame] = []
    rent_rows: list[pl.DataFrame] = []
    for index_key, index_matrix in index_blocks:
        (inflation_rows if isinstance(index_key, InflationKey) else rent_rows).append(row(index_key, index_matrix))

    home_value_rows = [row(property_key, property_matrix) for property_key, property_matrix in property_value_blocks]

    nominal_yield_rows: list[pl.DataFrame] = []
    muni_ratio_rows: list[pl.DataFrame] = []
    for rate_key, rate_matrix in discount_rate_blocks:
        target = nominal_yield_rows if isinstance(rate_key, NominalYieldKey) else muni_ratio_rows
        target.append(row(rate_key, rate_matrix))

    return LevelMagisteria(
        asset_prices=AssetPriceFrames(
            sp500=concat_frames(sp500_rows, SCALAR_LEVELS_SCHEMA),
            crypto=concat_frames(crypto_rows, SYMBOL_LEVELS_SCHEMA),
        ),
        property_values=concat_frames(home_value_rows, LOCATION_LEVELS_SCHEMA),
        index_series=IndexSeriesFrames(
            inflation=concat_frames(inflation_rows, SCALAR_LEVELS_SCHEMA),
            rent=concat_frames(rent_rows, LOCATION_LEVELS_SCHEMA),
        ),
        discount_rates=DiscountRateFrames(
            nominal_yield=concat_frames(nominal_yield_rows, TENOR_LEVELS_SCHEMA),
            muni_ratio=concat_frames(muni_ratio_rows, TENOR_LEVELS_SCHEMA),
        ),
    )


def merge_level_magisteria(left: SampledExogenousBundle, right: SampledExogenousBundle) -> LevelBundleKwargs:
    """Per-kind concat of two bundles' level frames, rejecting duplicate sub-ids / singleton collisions."""

    def merge(kind: LevelSeriesKind) -> pl.DataFrame:
        left_frame = left._frame_for_kind(kind)
        right_frame = right._frame_for_kind(kind)
        subid_column = _SUBID_COLUMN_BY_KIND[kind]
        if subid_column is None:
            if not left_frame.is_empty() and not right_frame.is_empty():
                raise ValueError(f"composite exogenous providers both produced the singleton {kind} series")
        else:
            _reject_duplicate_subids(left_frame, right_frame, subid_column=subid_column, label=f"{kind} series")
        return concat_frames([left_frame, right_frame], _SCHEMA_BY_KIND[kind])

    return LevelMagisteria(
        asset_prices=AssetPriceFrames(sp500=merge(LevelSeriesKind.SP500), crypto=merge(LevelSeriesKind.CRYPTO)),
        property_values=merge(LevelSeriesKind.HOME_VALUE),
        index_series=IndexSeriesFrames(inflation=merge(LevelSeriesKind.INFLATION), rent=merge(LevelSeriesKind.RENT)),
        discount_rates=DiscountRateFrames(
            nominal_yield=merge(LevelSeriesKind.NOMINAL_YIELD), muni_ratio=merge(LevelSeriesKind.MUNI_RATIO)
        ),
    ).as_bundle_kwargs()


def _reject_duplicate_subids(left: pl.DataFrame, right: pl.DataFrame, *, subid_column: str, label: str) -> None:
    duplicate = sorted(_string_values(left, subid_column) & _string_values(right, subid_column))
    if duplicate:
        raise ValueError(f"composite exogenous providers produced duplicate {label}: {duplicate}")


def level_value_rows(sampled: SampledExogenousBundle) -> list[tuple[LevelSeriesKey, pl.DataFrame]]:
    """Yield `(key, (rollout_index, month_index, value) frame)` for every distinct series.

    The model-side export the sim handoff builds its flat index from — the sim
    stamps `series_id = key.wire_id` (or builds a typed intern table). No
    `series_id` strings are constructed here.
    """

    rows: list[tuple[LevelSeriesKey, pl.DataFrame]] = []
    if not sampled.index_series.inflation.is_empty():
        rows.append((InflationKey(), sampled.index_series.inflation))
    if not sampled.asset_prices.sp500.is_empty():
        rows.append((SP500Key(), sampled.asset_prices.sp500))
    for symbol in sorted(_string_values(sampled.asset_prices.crypto, "symbol")):
        frame = sampled.asset_prices.crypto.filter(pl.col("symbol") == symbol).select(
            "rollout_index", "month_index", "value"
        )
        rows.append((CryptoKey(symbol=CryptoSymbol(symbol)), frame))
    for loc in sorted(_string_values(sampled.property_values, "location_id")):
        frame = sampled.property_values.filter(pl.col("location_id") == loc).select(
            "rollout_index", "month_index", "value"
        )
        rows.append((HomeValueKey(location_id=LocationId(loc)), frame))
    for loc in sorted(_string_values(sampled.index_series.rent, "location_id")):
        frame = sampled.index_series.rent.filter(pl.col("location_id") == loc).select(
            "rollout_index", "month_index", "value"
        )
        rows.append((RentKey(location_id=LocationId(loc)), frame))
    for rate_frame, rate_key_type in (
        (sampled.discount_rates.nominal_yield, NominalYieldKey),
        (sampled.discount_rates.muni_ratio, MuniRatioKey),
    ):
        for tenor in sorted(int(value) for value in _string_values(rate_frame, "tenor_months")):
            frame = rate_frame.filter(pl.col("tenor_months") == tenor).select("rollout_index", "month_index", "value")
            rows.append((rate_key_type(tenor_months=TenorMonths(tenor)), frame))
    return rows


def level_keys_in_bundle(sampled: SampledExogenousBundle) -> frozenset[LevelSeriesKey]:
    """The distinct typed keys present across all level magisteria."""

    return frozenset(key for key, _ in level_value_rows(sampled))


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
    frame = sampled._frame_for_kind(key.kind)
    if frame.is_empty():
        return False
    subid_column = _SUBID_COLUMN_BY_KIND[key.kind]
    if subid_column is None:
        return True
    return str(_key_subid(key)) in _string_values(frame, subid_column)


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
    anchors_by_kind: dict[LevelSeriesKind, dict[str | int | None, float]] = {kind: {} for kind in LevelSeriesKind}
    for key, value in level_anchors_typed.items():
        subid = None if _SUBID_COLUMN_BY_KIND[key.kind] is None else _key_subid(key)
        anchors_by_kind[key.kind][subid] = float(value)

    def rescale(kind: LevelSeriesKind) -> pl.DataFrame:
        return _anchor_level_frame(sampled._frame_for_kind(kind), kind, anchors_by_kind[kind])

    return SampledExogenousBundle(
        asset_prices=AssetPriceFrames(sp500=rescale(LevelSeriesKind.SP500), crypto=rescale(LevelSeriesKind.CRYPTO)),
        property_values=rescale(LevelSeriesKind.HOME_VALUE),
        index_series=IndexSeriesFrames(
            inflation=rescale(LevelSeriesKind.INFLATION), rent=rescale(LevelSeriesKind.RENT)
        ),
        discount_rates=DiscountRateFrames(
            nominal_yield=rescale(LevelSeriesKind.NOMINAL_YIELD), muni_ratio=rescale(LevelSeriesKind.MUNI_RATIO)
        ),
        private_equity=private_equity,
        metadata={**sampled.metadata, **metadata_extras},
    )


def _anchor_level_frame(
    frame: pl.DataFrame, kind: LevelSeriesKind, anchors_for_kind: Mapping[str | int | None, float]
) -> pl.DataFrame:
    """Re-base one per-kind frame so each series' per-rollout month-0 value matches its anchor.

    Two anchoring modes, by magisterium. Levels (prices, property values, indices) are
    **rescaled** — the path is multiplicative, so matching month 0 means scaling the whole
    series. Rates (`_ADDITIVELY_ANCHORED_KINDS`) are **shifted** — a yield curve is not a
    positive level, dividing by a near-zero month-0 yield would explode the path, and the
    financially meaningful operation is to move the sampled curve onto today's observed one.
    """

    if frame.is_empty() or not anchors_for_kind:
        return frame
    schema = _SCHEMA_BY_KIND[kind]
    subid_column = _SUBID_COLUMN_BY_KIND[kind]
    additive = kind in _ADDITIVELY_ANCHORED_KINDS

    def rebased(value: pl.Expr, base: pl.Expr, anchor: pl.Expr) -> pl.Expr:
        return value - base + anchor if additive else value * anchor / base

    if subid_column is None:
        anchor_value = next(iter(anchors_for_kind.values()))
        bases = frame.filter(pl.col("month_index") == 0).select("rollout_index", pl.col("value").alias("_base_value"))
        if not additive and not bases.filter(pl.col("_base_value") == 0.0).is_empty():
            raise ValueError(f"sampled series {kind!r} has zero month-0 value and cannot be anchored")
        return (
            frame.join(bases, on="rollout_index", how="left")
            .with_columns(value=rebased(pl.col("value"), pl.col("_base_value"), pl.lit(anchor_value)))
            .select(schema.names())
        )

    active = {sub: val for sub, val in anchors_for_kind.items() if sub is not None}
    if not active:
        return frame
    # The sub-id column's dtype is the frame's, not always Utf8 (tenor_months is Int64), so
    # the anchor frame is built against the schema rather than assuming strings.
    subid_dtype = schema[subid_column]
    subid_values: list[object] = [int(sub) if subid_dtype == pl.Int64() else sub for sub in active]
    anchor_frame = pl.DataFrame(
        {subid_column: subid_values, "_anchor_value": list(active.values())},
        schema={subid_column: subid_dtype, "_anchor_value": pl.Float64()},
    )
    bases = (
        frame.filter(pl.col("month_index") == 0)
        .join(anchor_frame, on=subid_column, how="inner")
        .select("rollout_index", subid_column, "_anchor_value", pl.col("value").alias("_base_value"))
    )
    if not additive:
        zero_bases = bases.filter(pl.col("_base_value") == 0.0)
        if not zero_bases.is_empty():
            bad = sorted(str(v) for v in set(zero_bases.get_column(subid_column).to_list()))
            raise ValueError(f"sampled {kind} series have zero month-0 value and cannot be anchored: {bad}")
    return (
        frame.join(bases, on=["rollout_index", subid_column], how="left")
        .with_columns(
            value=pl.when(pl.col("_anchor_value").is_not_null())
            .then(rebased(pl.col("value"), pl.col("_base_value"), pl.col("_anchor_value")))
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
