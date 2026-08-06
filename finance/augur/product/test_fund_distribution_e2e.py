"""A declared bond fund's distribution, end to end through `ProductService`.

The sim mechanic is covered at the sim level. What nothing covers is the SEAM: a deployment
declares what a fund is made of, a portfolio says where it is held, and the two have to meet
and reach a running engine. Every hop is a place the payout or its tax character could be
silently dropped — and a dropped payout produces a perfectly plausible projection, just one
where fixed income has no yield, so it cannot be caught by looking at the output alone.

The fund is layered onto the fixture deployment here rather than added to
`api/testdata/config.yaml`. Putting it in the shared fixture would shift the expected cash in
seventeen assertions that are about cash bands, tender capacity and rent, burying a bond
fund's payout inside totals it has nothing to do with. Asserting the payout directly is the
stronger check anyway.
"""

from __future__ import annotations

import pytest
import pytest_bazel
from more_itertools import one

from finance.augur.api.config import Config, DistributionTaxShareConfig, SecurityDistributionConfig
from finance.augur.api.portfolio import HoldingKind, HoldingTaxLotConfig, SecurityHoldingConfig
from finance.augur.model.deterministic import Constant
from finance.augur.model.independent import IndependentProviderConfig
from finance.augur.model.level_series_groups import SecurityDistributionGroups
from finance.augur.model.provider_config import CompositeProviderConfig, MirroringProviderConfig, ProviderConfig
from finance.augur.model.series import SecuritySymbol
from finance.augur.product.conftest import MakeProductService
from finance.augur.product.scenarios import PRIMARY_ACCOUNT_ID, security_distributions_from_portfolio
from finance.augur.product.service import ProductService
from finance.augur.product.wire import RolloutRequest, ScenarioKey

_SYMBOL = SecuritySymbol("bnd")
_UNITS = 2_000.0
_PRICE_USD = 73.0
_PER_UNIT_USD = 0.22
_MONTHLY_PAYOUT_USD = _UNITS * _PER_UNIT_USD

# An aggregate fund: part Treasury (state-exempt), part corporate (exempt nowhere). The mixed
# case a single tag cannot express, and the reason the declaration is a vector.
_AGGREGATE = (
    DistributionTaxShareConfig(fraction=0.4, issuer_jurisdiction_id="federal_us"),
    DistributionTaxShareConfig(fraction=0.6),
)
_ALL_TREASURY = (DistributionTaxShareConfig(fraction=1.0, issuer_jurisdiction_id="federal_us"),)
_ALL_CORPORATE = (DistributionTaxShareConfig(fraction=1.0),)


def _with_bond_fund_series(model: ProviderConfig, *, distributes: bool) -> ProviderConfig:
    """Add the fund's price — and optionally its payout — to the fixture's own macro model.

    Patched into the deployment's preset rather than replacing it with a hand-built sampler:
    the rest of the fixture portfolio still needs its prices, inflation and the PE issuer, and
    a stand-in that emitted only the fund would fail the request before reaching the engine.
    """

    # The fixture's preset is composite(mirroring(independent)); asserted rather than matched
    # so a fixture reshaped underneath this test fails here instead of silently patching nothing.
    assert isinstance(model, CompositeProviderConfig)
    macro = model.macro
    assert isinstance(macro, MirroringProviderConfig)
    inner = macro.model
    assert isinstance(inner, IndependentProviderConfig)
    return model.model_copy(
        update={
            "macro": macro.model_copy(
                update={
                    "model": inner.model_copy(
                        update={
                            "asset_prices": inner.asset_prices.model_copy(
                                update={
                                    "security": {**inner.asset_prices.security, _SYMBOL: Constant(value=_PRICE_USD)}
                                }
                            ),
                            "security_distributions": SecurityDistributionGroups(
                                security_distribution=({_SYMBOL: Constant(value=_PER_UNIT_USD)} if distributes else {})
                            ),
                        }
                    )
                }
            )
        }
    )


def _with_bond_fund(config: Config, tax_character: tuple[DistributionTaxShareConfig, ...] | None) -> Config:
    """Add the fund to the portfolio, and optionally declare that it distributes.

    The two are separable on purpose: holding a fund and knowing what it pays are different
    facts, and the undeclared arm is what proves the payout comes from the declaration rather
    than from merely holding something.
    """

    fixed = config.portfolio_sources.fixed
    portfolio = fixed.portfolio.model_copy(
        update={
            "holdings": (
                *fixed.portfolio.holdings,
                SecurityHoldingConfig(
                    position_id="bond_fund",
                    account_id="taxable_brokerage",
                    label="Aggregate Bond Fund",
                    symbol=_SYMBOL,
                    security_kind=HoldingKind.ETF,
                    unit_value_usd=_PRICE_USD,
                    lots=(
                        HoldingTaxLotConfig(
                            lot_id="bnd_2023_01",
                            holding_period_months_at_start=40,
                            quantity=_UNITS,
                            cost_basis_usd=150_000.0,
                        ),
                    ),
                ),
            )
        }
    )
    declarations = (
        () if tax_character is None else (SecurityDistributionConfig(symbol=_SYMBOL, tax_character=tax_character),)
    )
    return config.model_copy(
        update={
            "portfolio_sources": config.portfolio_sources.model_copy(
                update={"fixed": fixed.model_copy(update={"portfolio": portfolio})}
            ),
            "security_distributions": declarations,
            "models": {
                model_id: _with_bond_fund_series(model, distributes=tax_character is not None)
                for model_id, model in config.models.items()
            },
        }
    )


def _service(
    augur_config: Config,
    make_product_service: MakeProductService,
    tax_character: tuple[DistributionTaxShareConfig, ...] | None,
) -> ProductService:
    config = _with_bond_fund(augur_config, tax_character)
    return make_product_service(config.models[config.default_model_id].realize_model(), config=config)


def _cash_path(product: ProductService, *, horizon_months: int = 3) -> list[float]:
    scenario = ScenarioKey(
        model_id="current_model", horizon_months=horizon_months, monthly_spend_usd=1_000.0, spend_index="none"
    )
    detail = product.rollout(RolloutRequest(scenario=scenario, seed=7))
    # `Frame` is the untyped wire shape; cash is always a float, and asserting so here beats
    # threading `float | int | bool | str | None` through the arithmetic below.
    return [value for value in detail.rollout.monthly_metrics["cash_usd"] if isinstance(value, float)]


def test_a_declared_fund_pays_its_per_unit_distribution_into_cash(
    augur_config: Config, make_product_service: MakeProductService
) -> None:
    """`units x dollars_per_unit`, every month, on top of whatever else the month does.

    Both arms hold the same fund, so the difference between them is the declaration — the
    payout, not the position.
    """

    declared = _cash_path(_service(augur_config, make_product_service, _ALL_TREASURY))
    undeclared = _cash_path(_service(augur_config, make_product_service, None))

    gain = [after - before for before, after in zip(undeclared, declared, strict=True)]
    assert gain == [pytest.approx(month * _MONTHLY_PAYOUT_USD) for month in range(len(declared))]


def test_the_tax_character_fractions_reach_the_scenario(augur_config: Config) -> None:
    """The declaration's split survives the config-to-scenario conversion.

    Asserted on the conversion rather than on tax paid downstream: the fixture's only ordinary
    income is this payout, which the standard deduction absorbs entirely, so every split
    produces the same (zero) tax and a downstream comparison would pass for a conversion that
    dropped the fractions. The sim-level suite covers what the engine then does with them.
    """

    config = _with_bond_fund(augur_config, _AGGREGATE)
    distributions = security_distributions_from_portfolio(
        config.portfolio_sources.fixed.portfolio, config.security_distributions, primary_agent_id="agent_a"
    )

    assert [(slice_.fraction, slice_.issuer_jurisdiction_id) for slice_ in one(distributions).tax_character] == [
        (0.4, "federal_us"),
        (0.6, None),
    ]


def test_the_payout_is_scoped_to_the_pool_that_holds_it(augur_config: Config) -> None:
    """The units paid on come from one (owner, custody account, asset) pool, and the cash lands
    in a CASH account — portfolio accounts are custody accounts and carry no cash row, so a
    payout routed to one would have nowhere to go."""

    config = _with_bond_fund(augur_config, _ALL_TREASURY)
    distribution = one(
        security_distributions_from_portfolio(
            config.portfolio_sources.fixed.portfolio, config.security_distributions, primary_agent_id="agent_a"
        )
    )

    assert (distribution.holding_account_id, distribution.to_account_id) == ("taxable_brokerage", PRIMARY_ACCOUNT_ID)
    assert distribution.asset.wire_id == "security:bnd"


def test_a_declared_security_nobody_holds_contributes_nothing(augur_config: Config) -> None:
    """What makes the deployment's list a catalog rather than a per-portfolio duplicate."""

    config = augur_config.model_copy(
        update={
            "security_distributions": (
                SecurityDistributionConfig(symbol=SecuritySymbol("unheld"), tax_character=_ALL_TREASURY),
            )
        }
    )

    assert (
        security_distributions_from_portfolio(
            config.portfolio_sources.fixed.portfolio, config.security_distributions, primary_agent_id="agent_a"
        )
        == ()
    )


if __name__ == "__main__":
    pytest_bazel.main()
