"""Compose different products on the same sampled structural markets without resampling."""

from dataclasses import replace

import pytest
import pytest_bazel

from finance.augur.model.bond_fund import BondFundSpec, YieldCurve
from finance.augur.model.equity import EquitySpec
from finance.augur.model.exogenous import ExogenousSamplingRequest
from finance.augur.model.product_paths import construct_products
from finance.augur.model.series import SecuritySymbol
from finance.augur.model.testing import TEST_EQUITY, check_two_constructions
from finance.augur.x.models.structural_macro import (
    EquityProcess,
    MacroVarSpec,
    StructuralMacroModel,
    StructuralMacroProviderConfig,
)


@pytest.fixture
def structural() -> StructuralMacroModel:
    return StructuralMacroProviderConfig(
        macro_state=MacroVarSpec(
            initial_state=(-0.01, 0.03, 0.025),
            intercept=(0.003, 0.003, 0.0025),
            transition=((0.9, 0.0, 0.0), (0.0, 0.9, 0.0), (0.0, 0.0, 0.9)),
            shock_cholesky=((0.001, 0.0, 0.0), (0.0, 0.001, 0.0), (0.0, 0.0, 0.001)),
        ),
        initial_inflation_level=137.0,
        equity=EquityProcess(
            instrument=EquitySpec(symbol=TEST_EQUITY, initial_price_usd=517.3),
            monthly_log_return_mu=0.003,
            monthly_log_return_sigma=0.01,
            rate_beta=-0.7,
        ),
    ).realize_model()


def test_structural_markets_are_reusable_but_do_not_invent_credit(structural: StructuralMacroModel) -> None:
    request = ExogenousSamplingRequest(horizon_months=12, rollout_seeds=(71, 12))
    paths = structural.sample_market(request)
    check_two_constructions(paths)
    assert paths.provenance["rollout_seeds"] == (71, 12)
    assert paths.short_rate[0, 0] == -0.01
    with pytest.raises(ValueError, match="test_credit"):
        construct_products(
            paths,
            equity=None,
            instruments=(
                BondFundSpec(
                    symbol=SecuritySymbol("test_credit"), maturity_years=5, yield_curve=YieldCurve.CORPORATE_AAA
                ),
            ),
        )


def test_equity_requires_an_actual_equity_market_path(structural: StructuralMacroModel) -> None:
    paths = replace(
        structural.sample_market(ExogenousSamplingRequest(horizon_months=1, rollout_seeds=(1,))),
        equity_total_return_index=None,
    )
    with pytest.raises(ValueError, match=TEST_EQUITY):
        construct_products(paths, equity=EquitySpec(symbol=TEST_EQUITY, initial_price_usd=100.0), instruments=())


if __name__ == "__main__":
    pytest_bazel.main()
