from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pytest
import pytest_bazel

from augur.model.composite import CompositeModel
from augur.model.exogenous import (
    ExogenousSamplingRequest,
    SampledExogenousBundle,
    assemble_level_magisteria,
    level_series_request_channels,
    partition_level_blocks,
)
from augur.model.private_equity_bundle import PrivateEquityBundle
from augur.model.private_equity_protocol import neutral_private_equity_issuer_bundle
from augur.model.series import InflationKey, IssuerId, LevelSeriesKey


@dataclass(frozen=True)
class _StaticSampler:
    """Test fixture: emit a constant level frame + (optionally) one PE issuer bundle."""

    levels: dict[LevelSeriesKey, float] = field(default_factory=dict)
    pe_issuer_marks: dict[str, float] = field(default_factory=dict)
    sample_requests: list[ExogenousSamplingRequest] = field(default_factory=list)

    def sample(self, request: ExogenousSamplingRequest) -> SampledExogenousBundle:
        self.sample_requests.append(request)

        def matrix(value: float) -> np.ndarray:
            return np.full((request.rollout_count, request.horizon_months + 1), value, dtype=np.float64)

        # Route this fixture's flat constant levels into the three magisterium block-groups
        # (a typed fan-out, not a merge) before assembling the bundle frames.
        asset_price_blocks, property_value_blocks, index_blocks = partition_level_blocks(
            (key, matrix(value)) for key, value in self.levels.items()
        )
        frames = assemble_level_magisteria(
            asset_price_blocks=asset_price_blocks,
            property_value_blocks=property_value_blocks,
            index_blocks=index_blocks,
            rollout_count=request.rollout_count,
            horizon_months=request.horizon_months,
        )
        pe_parts = [
            neutral_private_equity_issuer_bundle(
                issuer_id,
                observed_mark=np.full((request.rollout_count, request.horizon_months + 1), mark, dtype=np.float64),
                tender_events=np.zeros((request.rollout_count, request.horizon_months + 1), dtype=np.bool_),
                rollout_count=request.rollout_count,
                horizon_months=request.horizon_months,
            )
            for issuer_id, mark in self.pe_issuer_marks.items()
        ]
        return SampledExogenousBundle(
            asset_prices=frames.asset_prices,
            property_values=frames.property_values,
            index_series=frames.index_series,
            private_equity=PrivateEquityBundle.combine(pe_parts) if pe_parts else PrivateEquityBundle.empty(),
        )


def test_composite_merges_macro_and_private_equity_series() -> None:
    macro = _StaticSampler(levels={InflationKey(): 1.0})
    private_equity = _StaticSampler(pe_issuer_marks={"private_company_a": 687.69})
    model = CompositeModel(macro=macro, private_equity=private_equity)
    request = ExogenousSamplingRequest(
        horizon_months=3,
        rollout_seeds=(7,),
        **level_series_request_channels(frozenset({InflationKey()})),
        required_private_equity_issuers=frozenset({IssuerId("private_company_a")}),
    )

    bundle = model.sample(request)

    assert bundle.level_matrix(InflationKey(), rollout_count=1, horizon_months=3)[0, 0] == 1.0
    assert bundle.private_equity.issuer_float_matrix(
        "private_company_a", "mark_usd_per_unit", rollout_count=1, horizon_months=3
    )[0, 0] == pytest.approx(687.69)
    # PE flows only to the PE sub-provider; the macro sub-provider sees an empty PE-issuer set.
    assert macro.sample_requests[0].required_level_series == frozenset({InflationKey()})
    assert macro.sample_requests[0].required_private_equity_issuers == frozenset()
    assert private_equity.sample_requests[0].required_private_equity_issuers == frozenset({"private_company_a"})


def test_composite_rejects_missing_required_private_equity_issuer() -> None:
    model = CompositeModel(
        macro=_StaticSampler(levels={InflationKey(): 1.0}),
        private_equity=_StaticSampler(pe_issuer_marks={"private_company_a": 687.69}),
    )

    with pytest.raises(ValueError, match=r"missing required private-equity issuer\(s\): \['different_issuer'\]"):
        model.sample(
            ExogenousSamplingRequest(
                horizon_months=1,
                rollout_seeds=(1,),
                required_private_equity_issuers=frozenset({IssuerId("different_issuer")}),
            )
        )


if __name__ == "__main__":
    pytest_bazel.main()
