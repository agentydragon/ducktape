"""Composite exogenous provider that merges macro and private-equity components."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import polars as pl

from augur.frames import concat_frames
from augur.model.exogenous import (
    SERIES_LEVELS_SCHEMA,
    ExogenousSamplingRequest,
    SampledExogenousBundle,
    Sampler,
    validate_sample_satisfies_request,
)
from augur.model.private_equity_bundle import PrivateEquityBundle


@dataclass(frozen=True)
class CompositeModel:
    """Route non-PE series to a macro provider and PE series/events to a PE provider."""

    macro: Sampler
    private_equity: Sampler
    label: str = "composite_exogenous_model"

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        # `required_level_series` only carries non-PE `LevelSeriesKey` values
        # post-migration; PE is routed via `required_private_equity_issuers`.
        macro_request = ExogenousSamplingRequest(
            horizon_months=request.horizon_months,
            rollout_seeds=request.rollout_seeds,
            required_level_series=request.required_level_series,
            required_private_equity_issuers=frozenset(),
        )
        pe_request = ExogenousSamplingRequest(
            horizon_months=request.horizon_months,
            rollout_seeds=request.rollout_seeds,
            required_level_series=frozenset(),
            required_private_equity_issuers=request.required_private_equity_issuers,
        )

        macro_bundle = self.macro.sample(macro_request)
        pe_bundle = self.private_equity.sample(pe_request)
        _reject_duplicate_ids(macro_bundle.levels, pe_bundle.levels, id_column="series_id", label="level series")
        sampled = SampledExogenousBundle(
            levels=concat_frames([macro_bundle.levels, pe_bundle.levels], SERIES_LEVELS_SCHEMA),
            private_equity=PrivateEquityBundle.combine([macro_bundle.private_equity, pe_bundle.private_equity]),
            metadata={
                "model_id": self.label,
                "private_equity_prices_usd": _private_equity_prices_usd(pe_bundle.metadata),
                "macro_metadata": dict(macro_bundle.metadata),
                "private_equity_metadata": dict(pe_bundle.metadata),
            },
        )
        validate_sample_satisfies_request(request, sampled)
        return sampled


def _reject_duplicate_ids(left: pl.DataFrame, right: pl.DataFrame, *, id_column: str, label: str) -> None:
    left_ids = _ids(left, id_column)
    right_ids = _ids(right, id_column)
    duplicate = sorted(left_ids & right_ids)
    if duplicate:
        raise ValueError(f"composite exogenous providers produced duplicate {label}: {duplicate}")


def _ids(frame: pl.DataFrame, column: str) -> frozenset[str]:
    if frame.is_empty():
        return frozenset()
    return frozenset(str(value) for value in frame.get_column(column).unique().to_list())


def _private_equity_prices_usd(metadata: Mapping[str, object]) -> dict[str, float]:
    raw = metadata.get("private_equity_prices_usd")
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise TypeError("private_equity metadata key private_equity_prices_usd must be a mapping")

    prices: dict[str, float] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise TypeError("private_equity_prices_usd keys must be strings")
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError(f"private_equity_prices_usd[{key!r}] must be numeric")
        prices[key] = float(value)
    return prices
