"""Composite exogenous provider that merges macro and private-equity components."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from finance.augur.model.exogenous import (
    ExogenousSamplingRequest,
    SampledExogenousBundle,
    Sampler,
    merge_level_magisteria,
    validate_sample_satisfies_request,
)
from finance.augur.model.private_equity_bundle import PrivateEquityBundle
from finance.augur.model.series import IssuerId, LevelSeriesKey


@dataclass(frozen=True)
class CompositeModel:
    """Route non-PE series to a macro provider and PE series/events to a PE provider."""

    macro: Sampler
    private_equity: Sampler
    label: str = "composite"

    def emittable_level_keys(self) -> frozenset[LevelSeriesKey]:
        return self.macro.emittable_level_keys() | self.private_equity.emittable_level_keys()

    def emittable_private_equity_issuers(self) -> frozenset[IssuerId]:
        return self.macro.emittable_private_equity_issuers() | self.private_equity.emittable_private_equity_issuers()

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        # Non-PE level series (all three magisteria) route to the macro provider;
        # PE routes via `required_private_equity_issuers` to the PE provider.
        macro_request = ExogenousSamplingRequest(
            horizon_months=request.horizon_months,
            rollout_seeds=request.rollout_seeds,
            required_asset_prices=request.required_asset_prices,
            required_property_values=request.required_property_values,
            required_index_series=request.required_index_series,
            required_private_equity_issuers=frozenset(),
        )
        pe_request = ExogenousSamplingRequest(
            horizon_months=request.horizon_months,
            rollout_seeds=request.rollout_seeds,
            required_private_equity_issuers=request.required_private_equity_issuers,
        )

        macro_bundle = self.macro.sample(macro_request)
        pe_bundle = self.private_equity.sample(pe_request)
        sampled = SampledExogenousBundle(
            **merge_level_magisteria(macro_bundle, pe_bundle),
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
