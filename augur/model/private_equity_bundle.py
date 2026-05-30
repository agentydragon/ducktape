"""Wide private-equity protocol bundle.

The PE protocol carries 10 conceptually-parallel channels per issuer:

| Channel                       | Type    | Meaning                                      |
| ----------------------------- | ------- | -------------------------------------------- |
| `mark_usd_per_unit`           | float   | Per-unit valuation / sale price              |
| `regime_code`                 | int     | `PrivateEquityRegimeCode`                    |
| `event_kind_code`             | int     | `PrivateEquityEventKindCode`                 |
| `sale_opportunity_active`     | bool    | Tender / public-market window active         |
| `sale_capacity_fraction`      | float   | Fraction of held units sellable on tender    |
| `eligible_fraction`           | float   | Fraction of held units eligible for sale     |
| `forced_sale_fraction`        | float   | Fraction forcibly sold in that month         |
| `liquidity_blocked`           | bool    | Voluntary sales blocked                       |
| `forced_recovery_cashout_usd` | float   | Dollar recovery for the remaining position   |
| `company_valuation_usd`       | float   | Company market cap `V(t)` (0 == channel off) |

Producers used to emit these as 8 separate level series + 1 event series +
a separate typed protocol frame, validated together at sim compile time.
The all-or-nothing invariant is now expressed by the schema of one wide
polars DataFrame: every channel is a required column, and producers must
go through `PrivateEquityBundle.from_issuer_arrays` which keyword-only
requires every channel matrix. Missing any one is a `TypeError`, not a
sim-compile validation error.

`company_valuation_usd` is the M2 coupled-valuation channel.  It carries the
sampled company market cap `V(t)` and is all-zeros for issuers whose valuation
channel is disabled (no `current_valuation_usd` anchor) — zero is a valid
sentinel for "no valuation modeled", distinct from any positive market cap.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

import numpy as np
import numpy.typing as npt
import polars as pl

from augur.frames import FrameSpec
from augur.model.series import IssuerId, PrivateEquityEventKindCode, PrivateEquityRegimeCode


class PrivateEquityFloatChannel(StrEnum):
    """Float-valued PE bundle channel column names."""

    MARK_USD_PER_UNIT = "mark_usd_per_unit"
    SALE_CAPACITY_FRACTION = "sale_capacity_fraction"
    ELIGIBLE_FRACTION = "eligible_fraction"
    FORCED_SALE_FRACTION = "forced_sale_fraction"
    FORCED_RECOVERY_CASHOUT_USD = "forced_recovery_cashout_usd"
    COMPANY_VALUATION_USD = "company_valuation_usd"


class PrivateEquityIntChannel(StrEnum):
    """Integer-valued PE bundle channel column names — discriminated codes."""

    REGIME_CODE = "regime_code"
    EVENT_KIND_CODE = "event_kind_code"


class PrivateEquityBoolChannel(StrEnum):
    """Boolean-valued PE bundle channel column names."""

    SALE_OPPORTUNITY_ACTIVE = "sale_opportunity_active"
    LIQUIDITY_BLOCKED = "liquidity_blocked"


type PrivateEquityChannel = PrivateEquityFloatChannel | PrivateEquityIntChannel | PrivateEquityBoolChannel

PRIVATE_EQUITY_BUNDLE_SCHEMA = pl.Schema(
    {
        "rollout_index": pl.Int64(),
        "month_index": pl.Int64(),
        "issuer_id": pl.Utf8(),
        "mark_usd_per_unit": pl.Float64(),
        "regime_code": pl.Int64(),
        "event_kind_code": pl.Int64(),
        "sale_opportunity_active": pl.Boolean(),
        "sale_capacity_fraction": pl.Float64(),
        "eligible_fraction": pl.Float64(),
        "forced_sale_fraction": pl.Float64(),
        "liquidity_blocked": pl.Boolean(),
        "forced_recovery_cashout_usd": pl.Float64(),
        "company_valuation_usd": pl.Float64(),
    }
)

PRIVATE_EQUITY_BUNDLE_FRAME = FrameSpec("private_equity_bundle", PRIVATE_EQUITY_BUNDLE_SCHEMA)

# Columns exposed for per-issuer matrix views.
_FLOAT_COLUMNS = frozenset(
    {
        "mark_usd_per_unit",
        "sale_capacity_fraction",
        "eligible_fraction",
        "forced_sale_fraction",
        "forced_recovery_cashout_usd",
        "company_valuation_usd",
    }
)
_INT_COLUMNS = frozenset({"regime_code", "event_kind_code"})
_BOOL_COLUMNS = frozenset({"sale_opportunity_active", "liquidity_blocked"})

type FloatMatrix = npt.NDArray[np.float64]
type IntMatrix = npt.NDArray[np.int64]
type BoolMatrix = npt.NDArray[np.bool_]


@dataclass(frozen=True)
class PrivateEquityBundle:
    """One polars DataFrame carrying every PE protocol channel for every issuer.

    Frame shape: `(rollout_index, month_index, issuer_id)` × the 9 typed channels.
    Empty case: a frame matching the schema with zero rows.
    """

    frame: pl.DataFrame

    def __post_init__(self) -> None:
        if self.frame.schema != PRIVATE_EQUITY_BUNDLE_SCHEMA:
            raise ValueError(
                f"private_equity_bundle schema must be {PRIVATE_EQUITY_BUNDLE_SCHEMA}, got {self.frame.schema}"
            )

    @classmethod
    def empty(cls) -> PrivateEquityBundle:
        return cls(frame=PRIVATE_EQUITY_BUNDLE_FRAME.empty())

    @classmethod
    def from_issuer_arrays(
        cls,
        issuer_id: IssuerId | str,
        *,
        mark_usd_per_unit: FloatMatrix,
        regime_code: IntMatrix,
        event_kind_code: IntMatrix,
        sale_opportunity_active: BoolMatrix,
        sale_capacity_fraction: FloatMatrix,
        eligible_fraction: FloatMatrix,
        forced_sale_fraction: FloatMatrix,
        liquidity_blocked: BoolMatrix,
        forced_recovery_cashout_usd: FloatMatrix,
        company_valuation_usd: FloatMatrix,
        rollout_count: int,
        horizon_months: int,
    ) -> PrivateEquityBundle:
        """Build a single-issuer bundle. Every channel is a required keyword.

        ``company_valuation_usd`` is the M2 coupled-valuation channel; producers
        with no valuation concept pass an all-zeros matrix (channel off). Zeros
        are valid (non-negative); only negatives are rejected.
        """

        expected_shape = (rollout_count, horizon_months + 1)
        _require_float_matrix(mark_usd_per_unit, expected_shape, "mark_usd_per_unit")
        if np.any(mark_usd_per_unit < 0.0):
            raise ValueError(f"PE issuer {issuer_id!r} mark_usd_per_unit must be non-negative")
        _require_int_matrix(regime_code, expected_shape, "regime_code")
        _require_int_matrix(event_kind_code, expected_shape, "event_kind_code")
        _require_bool_matrix(sale_opportunity_active, expected_shape, "sale_opportunity_active")
        _require_unit_interval(sale_capacity_fraction, expected_shape, "sale_capacity_fraction")
        _require_unit_interval(eligible_fraction, expected_shape, "eligible_fraction")
        _require_unit_interval(forced_sale_fraction, expected_shape, "forced_sale_fraction")
        _require_bool_matrix(liquidity_blocked, expected_shape, "liquidity_blocked")
        _require_float_matrix(forced_recovery_cashout_usd, expected_shape, "forced_recovery_cashout_usd")
        if np.any(forced_recovery_cashout_usd < 0.0):
            raise ValueError(f"PE issuer {issuer_id!r} forced_recovery_cashout_usd must be non-negative")
        _require_float_matrix(company_valuation_usd, expected_shape, "company_valuation_usd")
        # Market cap is non-negative; all-zeros means the valuation channel is off.
        if np.any(company_valuation_usd < 0.0):
            raise ValueError(f"PE issuer {issuer_id!r} company_valuation_usd must be non-negative")
        _require_code_values(regime_code, frozenset(int(c) for c in PrivateEquityRegimeCode), "regime_code")
        _require_code_values(event_kind_code, frozenset(int(c) for c in PrivateEquityEventKindCode), "event_kind_code")
        # Invariant: a voluntary tender opportunity (`sale_opportunity_active`) is the same
        # thing as the TENDER event kind. Producers may not desync the two.
        tender_mask = event_kind_code == int(PrivateEquityEventKindCode.TENDER)
        if np.any(tender_mask != sale_opportunity_active):
            raise ValueError(
                f"PE issuer {issuer_id!r}: event_kind_code==TENDER must coincide with sale_opportunity_active==True"
            )

        rollout_idx = np.repeat(np.arange(rollout_count, dtype=np.int64), horizon_months + 1)
        month_idx = np.tile(np.arange(horizon_months + 1, dtype=np.int64), rollout_count)
        row_count = rollout_count * (horizon_months + 1)
        frame = pl.DataFrame(
            {
                "rollout_index": rollout_idx,
                "month_index": month_idx,
                "issuer_id": [str(issuer_id)] * row_count,
                "mark_usd_per_unit": mark_usd_per_unit.reshape(-1),
                "regime_code": regime_code.reshape(-1),
                "event_kind_code": event_kind_code.reshape(-1),
                "sale_opportunity_active": sale_opportunity_active.reshape(-1),
                "sale_capacity_fraction": sale_capacity_fraction.reshape(-1),
                "eligible_fraction": eligible_fraction.reshape(-1),
                "forced_sale_fraction": forced_sale_fraction.reshape(-1),
                "liquidity_blocked": liquidity_blocked.reshape(-1),
                "forced_recovery_cashout_usd": forced_recovery_cashout_usd.reshape(-1),
                "company_valuation_usd": company_valuation_usd.reshape(-1),
            },
            schema=PRIVATE_EQUITY_BUNDLE_SCHEMA,
        )
        return cls(frame=frame)

    @classmethod
    def combine(cls, parts: Iterable[PrivateEquityBundle]) -> PrivateEquityBundle:
        """Concatenate per-issuer bundles. Rejects duplicate issuer ids."""

        frames = [part.frame for part in parts]
        seen: set[str] = set()
        for frame in frames:
            for issuer in _issuer_ids_in_frame(frame):
                if issuer in seen:
                    raise ValueError(f"private-equity bundle has duplicate issuer {issuer!r}")
                seen.add(issuer)
        return cls(frame=PRIVATE_EQUITY_BUNDLE_FRAME.concat(frames))

    def issuer_ids(self) -> frozenset[IssuerId]:
        return frozenset(IssuerId(value) for value in _issuer_ids_in_frame(self.frame))

    def is_empty(self) -> bool:
        return self.frame.is_empty()

    def issuer_float_matrix(
        self, issuer_id: IssuerId | str, column: str, *, rollout_count: int, horizon_months: int
    ) -> FloatMatrix:
        if column not in _FLOAT_COLUMNS:
            raise ValueError(f"{column!r} is not a float column of PrivateEquityBundle")
        return self._issuer_matrix(
            issuer_id, column, np.float64, rollout_count=rollout_count, horizon_months=horizon_months
        )

    def issuer_int_matrix(
        self, issuer_id: IssuerId | str, column: str, *, rollout_count: int, horizon_months: int
    ) -> IntMatrix:
        if column not in _INT_COLUMNS:
            raise ValueError(f"{column!r} is not an int column of PrivateEquityBundle")
        return self._issuer_matrix(
            issuer_id, column, np.int64, rollout_count=rollout_count, horizon_months=horizon_months
        )

    def issuer_bool_matrix(
        self, issuer_id: IssuerId | str, column: str, *, rollout_count: int, horizon_months: int
    ) -> BoolMatrix:
        if column not in _BOOL_COLUMNS:
            raise ValueError(f"{column!r} is not a bool column of PrivateEquityBundle")
        return self._issuer_matrix(
            issuer_id, column, np.bool_, rollout_count=rollout_count, horizon_months=horizon_months
        )

    def _issuer_matrix(
        self,
        issuer_id: IssuerId | str,
        column: str,
        dtype: type[np.generic],
        *,
        rollout_count: int,
        horizon_months: int,
    ) -> npt.NDArray:
        selected = self.frame.filter(pl.col("issuer_id") == str(issuer_id)).sort(["rollout_index", "month_index"])
        if selected.is_empty():
            raise KeyError(f"private-equity bundle has no rows for issuer {issuer_id!r}")
        expected_rows = rollout_count * (horizon_months + 1)
        if selected.height != expected_rows:
            raise ValueError(
                f"private-equity bundle for issuer {issuer_id!r} has {selected.height} rows; expected {expected_rows}"
            )
        return selected.get_column(column).to_numpy().astype(dtype).reshape((rollout_count, horizon_months + 1))


def _issuer_ids_in_frame(frame: pl.DataFrame) -> list[str]:
    if frame.is_empty():
        return []
    return sorted(str(value) for value in frame.get_column("issuer_id").unique().to_list())


def _require_float_matrix(value: np.ndarray, expected_shape: tuple[int, int], label: str) -> None:
    if value.shape != expected_shape:
        raise ValueError(f"private-equity bundle channel {label!r} has shape {value.shape}; expected {expected_shape}")
    if not np.issubdtype(value.dtype, np.floating):
        raise ValueError(f"private-equity bundle channel {label!r} must have a float dtype")
    if not np.isfinite(value).all():
        raise ValueError(f"private-equity bundle channel {label!r} must be finite")


def _require_int_matrix(value: np.ndarray, expected_shape: tuple[int, int], label: str) -> None:
    if value.shape != expected_shape:
        raise ValueError(f"private-equity bundle channel {label!r} has shape {value.shape}; expected {expected_shape}")
    if not np.issubdtype(value.dtype, np.integer):
        raise ValueError(f"private-equity bundle channel {label!r} must have an integer dtype")


def _require_bool_matrix(value: np.ndarray, expected_shape: tuple[int, int], label: str) -> None:
    if value.shape != expected_shape:
        raise ValueError(f"private-equity bundle channel {label!r} has shape {value.shape}; expected {expected_shape}")
    if value.dtype != np.bool_:
        raise ValueError(f"private-equity bundle channel {label!r} must have a bool dtype")


def _require_unit_interval(value: np.ndarray, expected_shape: tuple[int, int], label: str) -> None:
    _require_float_matrix(value, expected_shape, label)
    if np.any(value < 0.0) or np.any(value > 1.0):
        raise ValueError(f"private-equity bundle channel {label!r} must lie in [0, 1]")


def _require_code_values(value: np.ndarray, allowed: frozenset[int], label: str) -> None:
    unknown = sorted(int(code) for code in np.unique(value) if int(code) not in allowed)
    if unknown:
        raise ValueError(f"private-equity bundle channel {label!r} has unknown code(s) {unknown}")
