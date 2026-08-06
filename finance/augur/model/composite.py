"""Composite exogenous provider that merges macro and private-equity components."""

from __future__ import annotations

from dataclasses import dataclass, replace

from finance.augur.model.exogenous import (
    ExogenousSamplingRequest,
    SampledExogenousBundle,
    Sampler,
    merge_level_frames,
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
        # Non-PE level series (every role) route to the macro provider;
        # PE routes via `required_private_equity_issuers` to the PE provider. `replace` rather
        # than a field-by-field rebuild: "every role" has to stay true when a role is added,
        # and a rebuild that forgot one would silently ask the macro provider for less.
        macro_request = replace(request, required_private_equity_issuers=frozenset())
        pe_request = ExogenousSamplingRequest(
            horizon_months=request.horizon_months,
            rollout_seeds=request.rollout_seeds,
            required_private_equity_issuers=request.required_private_equity_issuers,
        )

        macro_bundle = self.macro.sample(macro_request)
        pe_bundle = self.private_equity.sample(pe_request)
        sampled = SampledExogenousBundle(
            levels=merge_level_frames(macro_bundle.levels, pe_bundle.levels),
            private_equity=PrivateEquityBundle.combine([macro_bundle.private_equity, pe_bundle.private_equity]),
            # The PE half's month-0 marks pass straight through: it is the only component that
            # has any, and the field is already `Mapping[IssuerId, float]` on both sides. This
            # used to be fifteen lines of isinstance checks rebuilding a type the writer had.
            private_equity_prices_usd=pe_bundle.private_equity_prices_usd,
            model_id=self.label,
            provenance={
                "macro_provenance": dict(macro_bundle.provenance),
                "private_equity_provenance": dict(pe_bundle.provenance),
            },
        )
        validate_sample_satisfies_request(request, sampled)
        return sampled
