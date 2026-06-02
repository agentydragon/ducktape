"""Test-only exogenous path model fixtures."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt

from augur.model.exogenous import ExogenousSamplingRequest, SampledExogenousBundle, assemble_level_magisteria
from augur.model.private_equity_bundle import PrivateEquityBundle
from augur.model.series import IssuerId, LevelSeriesKey, PrivateEquityEventKindCode, PrivateEquityRegimeCode

type LevelOverride = float | npt.NDArray[np.float64] | Callable[[ExogenousSamplingRequest], npt.NDArray[np.float64]]
type IntOverride = int | npt.NDArray[np.int64] | Callable[[ExogenousSamplingRequest], npt.NDArray[np.int64]]
type EventOverride = bool | npt.NDArray[np.bool_] | Callable[[ExogenousSamplingRequest], npt.NDArray[np.bool_]]


@dataclass(frozen=True)
class PrivateEquityChannels:
    """Constant per-issuer PE channels for fixture sampling.

    `mark_usd_per_unit` is required; every other channel has a neutral
    default (`PRIVATE_OPERATING` regime, no events, full capacity /
    eligibility, no forced sale, no liquidity block, no recovery cashout,
    and the opt-in M2 company-valuation channel off — `company_valuation_usd`
    all-zeros).
    """

    mark_usd_per_unit: LevelOverride
    regime_code: IntOverride = int(PrivateEquityRegimeCode.PRIVATE_OPERATING)
    event_kind_code: IntOverride = int(PrivateEquityEventKindCode.NONE)
    sale_opportunity_active: EventOverride = False
    sale_capacity_fraction: LevelOverride = 1.0
    eligible_fraction: LevelOverride = 1.0
    forced_sale_fraction: LevelOverride = 0.0
    liquidity_blocked: EventOverride = False
    forced_recovery_cashout_usd: LevelOverride = 0.0
    company_valuation_usd: LevelOverride = 0.0


@dataclass
class ConstantFrameModel:
    """Constant-frame fixture sampler.

    `levels` and `private_equity` are the constants the sampler returns
    for each requested key. Sampling a key the fixture wasn't seeded
    with raises `KeyError` — there are no implicit fallbacks.
    """

    levels: Mapping[LevelSeriesKey, LevelOverride] = field(default_factory=dict)
    private_equity: Mapping[IssuerId, PrivateEquityChannels] = field(default_factory=dict)
    metadata: Mapping[str, object] = field(default_factory=lambda: {"model_id": "constant_frame_fixture"})
    sample_requests: list[ExogenousSamplingRequest] = field(default_factory=list)

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        self.sample_requests.append(request)

        def blocks[KeyT: LevelSeriesKey](keys: frozenset[KeyT]) -> list[tuple[KeyT, np.ndarray]]:
            return [
                (key, _level_matrix(self._require_level(key), request))
                for key in sorted(keys, key=lambda key: key.wire_id)
            ]

        # Sample each of the request's three typed channels separately; the bundle's
        # magisteria never pass through one merged level-key set.
        frames = assemble_level_magisteria(
            asset_price_blocks=blocks(request.required_asset_prices),
            property_value_blocks=blocks(request.required_property_values),
            index_blocks=blocks(request.required_index_series),
            rollout_count=request.rollout_count,
            horizon_months=request.horizon_months,
        )
        pe_parts = [
            _pe_bundle_from_channels(issuer_id, self._require_pe(issuer_id), request)
            for issuer_id in sorted(request.required_private_equity_issuers)
        ]
        return SampledExogenousBundle(
            **frames.as_bundle_kwargs(),
            private_equity=PrivateEquityBundle.combine(pe_parts) if pe_parts else PrivateEquityBundle.empty(),
            metadata=dict(self.metadata),
        )

    def _require_level(self, key: LevelSeriesKey) -> LevelOverride:
        if key not in self.levels:
            raise KeyError(f"constant fixture has no value seeded for level series {key.wire_id!r}")
        return self.levels[key]

    def _require_pe(self, issuer_id: IssuerId) -> PrivateEquityChannels:
        if issuer_id not in self.private_equity:
            raise KeyError(f"constant fixture has no PrivateEquityChannels seeded for issuer {issuer_id!r}")
        return self.private_equity[issuer_id]


def _pe_bundle_from_channels(
    issuer_id: str, channels: PrivateEquityChannels, request: ExogenousSamplingRequest
) -> PrivateEquityBundle:
    return PrivateEquityBundle.from_issuer_arrays(
        issuer_id,
        mark_usd_per_unit=_level_matrix(channels.mark_usd_per_unit, request),
        regime_code=_int_matrix(channels.regime_code, request),
        event_kind_code=_int_matrix(channels.event_kind_code, request),
        sale_opportunity_active=_event_matrix(channels.sale_opportunity_active, request),
        sale_capacity_fraction=_level_matrix(channels.sale_capacity_fraction, request),
        eligible_fraction=_level_matrix(channels.eligible_fraction, request),
        forced_sale_fraction=_level_matrix(channels.forced_sale_fraction, request),
        liquidity_blocked=_event_matrix(channels.liquidity_blocked, request),
        forced_recovery_cashout_usd=_level_matrix(channels.forced_recovery_cashout_usd, request),
        company_valuation_usd=_level_matrix(channels.company_valuation_usd, request),
        rollout_count=request.rollout_count,
        horizon_months=request.horizon_months,
    )


def level_matrix_with_month_override(*, default: float, override: float, month: int) -> LevelOverride:
    def build(request: ExogenousSamplingRequest) -> npt.NDArray[np.float64]:
        matrix = np.full((request.rollout_count, request.horizon_months + 1), default, dtype=np.float64)
        matrix[:, min(month, request.horizon_months)] = override
        return matrix

    return build


def level_matrix_with_step(*, default: float, override: float, month: int) -> LevelOverride:
    def build(request: ExogenousSamplingRequest) -> npt.NDArray[np.float64]:
        matrix = np.full((request.rollout_count, request.horizon_months + 1), default, dtype=np.float64)
        matrix[:, min(month, request.horizon_months) :] = override
        return matrix

    return build


def event_matrix_with_month_override(*, default: bool, override: bool, month: int) -> EventOverride:
    def build(request: ExogenousSamplingRequest) -> npt.NDArray[np.bool_]:
        matrix = np.full((request.rollout_count, request.horizon_months + 1), default, dtype=np.bool_)
        matrix[:, min(month, request.horizon_months)] = override
        return matrix

    return build


def event_matrix_with_step(*, default: bool, override: bool, month: int) -> EventOverride:
    def build(request: ExogenousSamplingRequest) -> npt.NDArray[np.bool_]:
        matrix = np.full((request.rollout_count, request.horizon_months + 1), default, dtype=np.bool_)
        matrix[:, min(month, request.horizon_months) :] = override
        return matrix

    return build


def int_matrix_with_month_override(*, default: int, override: int, month: int) -> IntOverride:
    def build(request: ExogenousSamplingRequest) -> npt.NDArray[np.int64]:
        matrix = np.full((request.rollout_count, request.horizon_months + 1), default, dtype=np.int64)
        matrix[:, min(month, request.horizon_months)] = override
        return matrix

    return build


def int_matrix_with_step(*, default: int, override: int, month: int) -> IntOverride:
    def build(request: ExogenousSamplingRequest) -> npt.NDArray[np.int64]:
        matrix = np.full((request.rollout_count, request.horizon_months + 1), default, dtype=np.int64)
        matrix[:, min(month, request.horizon_months) :] = override
        return matrix

    return build


def _level_matrix(value: LevelOverride, request: ExogenousSamplingRequest) -> npt.NDArray[np.float64]:
    raw = value(request) if callable(value) else value
    matrix = (
        np.asarray(raw, dtype=np.float64)
        if isinstance(raw, np.ndarray)
        else np.full((request.rollout_count, request.horizon_months + 1), float(raw), dtype=np.float64)
    )
    _check_shape(matrix, request)
    return matrix


def _int_matrix(value: IntOverride, request: ExogenousSamplingRequest) -> npt.NDArray[np.int64]:
    raw = value(request) if callable(value) else value
    matrix = (
        np.asarray(raw, dtype=np.int64)
        if isinstance(raw, np.ndarray)
        else np.full((request.rollout_count, request.horizon_months + 1), int(raw), dtype=np.int64)
    )
    _check_shape(matrix, request)
    return matrix


def _event_matrix(value: EventOverride, request: ExogenousSamplingRequest) -> npt.NDArray[np.bool_]:
    raw = value(request) if callable(value) else value
    matrix = (
        np.asarray(raw, dtype=np.bool_)
        if isinstance(raw, np.ndarray)
        else np.full((request.rollout_count, request.horizon_months + 1), bool(raw), dtype=np.bool_)
    )
    _check_shape(matrix, request)
    return matrix


def _check_shape(matrix: np.ndarray, request: ExogenousSamplingRequest) -> None:
    expected = (request.rollout_count, request.horizon_months + 1)
    if matrix.shape != expected:
        raise ValueError(f"constant fixture matrix has shape {matrix.shape}; expected {expected}")
