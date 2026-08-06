"""Sim-level e2e for fund distributions: a per-unit payout is cash, and it is interest.

The mechanic a stock/bond mix needs and a held ladder cannot give: fixed income as a marked,
rebalanceable sleeve. A bond fund's return is its distributions plus its price change, so
without the payout the fund is modeled as price-only and its whole yield disappears.

Deterministic per-unit series throughout, so every assertion is an exact dollar amount. A
band would pass a payout that was off by the tax split, which is the part that decides
Treasury against municipal.
"""

from __future__ import annotations

from itertools import pairwise

import polars as pl
import pytest
import pytest_bazel

from finance.augur.model.deterministic import Constant
from finance.augur.model.level_series_groups import AssetPriceGroups, SecurityDistributionGroups
from finance.augur.model.series import SecuritySymbol
from finance.augur.model.series_model import SeriesModelBundle
from finance.augur.sim.scenario import (
    Agent,
    DistributionTaxSlice,
    FilingStatus,
    InitialAccountBalance,
    InitialLot,
    Scenario,
    SecurityDistribution,
    TaxProfile,
)
from finance.augur.sim.simulate import simulate

_HORIZON = 13
_SYMBOL = SecuritySymbol("bnd")
_FUND = {"kind": "security", "symbol": _SYMBOL}
_UNITS = 10_000.0
_PRICE_USD = 73.0
# A round monthly payout per unit, so `units x per_unit` is exact at every split.
_PER_UNIT_USD = 0.20
_MONTHLY_PAYOUT_USD = _UNITS * _PER_UNIT_USD


def _bundle() -> SeriesModelBundle:
    """Both series the fund needs: a price for the lot, and a payout per unit.

    Two series of the same shape and units, which is the point of the payout being a
    primitive — nothing here is a rate and nothing downstream divides.
    """

    return SeriesModelBundle.independent(
        asset_prices=AssetPriceGroups(security={_SYMBOL: Constant(value=_PRICE_USD)}),
        security_distributions=SecurityDistributionGroups(
            security_distribution={_SYMBOL: Constant(value=_PER_UNIT_USD)}
        ),
    )


def _scenario(
    *, tax_character: tuple[DistributionTaxSlice, ...], taxed: bool = True, distributes: bool = True
) -> Scenario:
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id="alice", account_id="checking", balance_usd=50_000.0),
            InitialAccountBalance(agent_id="irs", account_id="checking", balance_usd=0.0),
        ],
        initial_lots=[
            InitialLot(
                lot_id="bnd_lot",
                agent_id="alice",
                account_id="brokerage",
                asset=_FUND,
                purchase_month_index=-24,
                quantity=_UNITS,
                cost_basis_per_unit_usd=_PRICE_USD,
            )
        ],
        security_distributions=[
            SecurityDistribution(
                asset=_FUND,
                agent_id="alice",
                holding_account_id="brokerage",
                to_account_id="checking",
                tax_character=tax_character,
            )
        ]
        if distributes
        else [],
        tax_profiles=[
            TaxProfile(
                agent_id="alice",
                filing_status=FilingStatus.SINGLE,
                jurisdiction_ids=["federal_us", "california"],
                tax_authority_agent_id="irs",
            )
        ]
        if taxed
        else [],
        external_series=_bundle(),
        horizon_months=_HORIZON,
    )


_TREASURY = (DistributionTaxSlice(fraction=1.0, issuer_jurisdiction_id="federal_us"),)
_CALIFORNIA_MUNI = (DistributionTaxSlice(fraction=1.0, issuer_jurisdiction_id="california"),)
_CORPORATE = (DistributionTaxSlice(fraction=1.0),)
# An aggregate fund: part Treasury, part corporate. The case a single tag cannot express.
_AGGREGATE = (
    DistributionTaxSlice(fraction=0.4, issuer_jurisdiction_id="federal_us"),
    DistributionTaxSlice(fraction=0.6),
)


def _cash_by_month(*, tax_character: tuple[DistributionTaxSlice, ...], distributes: bool = True) -> dict[int, float]:
    """Cash change attributable to each simulated month, untaxed so the payout stands alone."""

    run = simulate(
        _scenario(tax_character=tax_character, taxed=False, distributes=distributes), rollout_count=1, locations={}
    )
    balances = [
        float(value)
        for value in run.cash_balances.filter((pl.col("agent_id") == "alice") & (pl.col("account_id") == "checking"))
        .sort("month_index")
        .get_column("balance_usd")
        .to_list()
    ]
    return {month: round(after - before, 2) for month, (before, after) in enumerate(pairwise(balances))}


def _tax_by_jurisdiction(*, tax_character: tuple[DistributionTaxSlice, ...]) -> dict[str, float]:
    run = simulate(_scenario(tax_character=tax_character), rollout_count=1, locations={})
    return {
        str(row["jurisdiction_id"]): float(row["amount_usd"])
        for row in run.events_log.tax_accruals.group_by("jurisdiction_id").agg(pl.col("amount_usd").sum()).to_dicts()
    }


def test_the_payout_is_units_times_dollars_per_unit_every_month() -> None:
    """Monthly, unlike a semiannual coupon, and sized off units held rather than a rate on
    market value — which is why nothing in the engine needs the price to compute it."""

    deltas = _cash_by_month(tax_character=_TREASURY)

    assert deltas == dict.fromkeys(range(_HORIZON), _MONTHLY_PAYOUT_USD)


def test_splitting_the_tax_character_does_not_change_what_reaches_cash() -> None:
    """The slices are a tax decomposition, not separate payouts. They share one destination
    account, so this is also what catches a scatter that overwrote instead of accumulating."""

    assert _cash_by_month(tax_character=_AGGREGATE) == _cash_by_month(tax_character=_TREASURY)


def test_a_holding_with_no_declared_distribution_pays_nothing() -> None:
    """Guards the assertion above against passing for some unrelated reason: the same lot,
    the same sampled series, no payout declared, no cash."""

    assert set(_cash_by_month(tax_character=_TREASURY, distributes=False).values()) == {0.0}


def test_a_treasury_funds_distribution_is_federally_taxed_and_california_exempt() -> None:
    tax = _tax_by_jurisdiction(tax_character=_TREASURY)

    assert tax["federal_us"] > 0.0
    assert tax["california"] == 0.0


def test_an_in_state_muni_funds_distribution_is_exempt_everywhere() -> None:
    tax = _tax_by_jurisdiction(tax_character=_CALIFORNIA_MUNI)

    assert tax["federal_us"] == 0.0
    assert tax["california"] == 0.0


def test_a_mixed_fund_is_exempt_only_on_its_treasury_slice() -> None:
    """The reason the tax character is a vector. California taxes the corporate 60% and not
    the Treasury 40%, so a mixed fund owes strictly between the all-Treasury and all-corporate
    cases — a number neither single tag can produce."""

    mixed = _tax_by_jurisdiction(tax_character=_AGGREGATE)
    treasury = _tax_by_jurisdiction(tax_character=_TREASURY)
    corporate = _tax_by_jurisdiction(tax_character=_CORPORATE)

    assert treasury["california"] < mixed["california"] < corporate["california"]
    # Federal taxes both slices, so the split changes nothing there.
    assert mixed["federal_us"] == treasury["federal_us"] == corporate["federal_us"]


def test_the_payout_accrues_as_interest_per_issuer_not_as_one_lump() -> None:
    run = simulate(_scenario(tax_character=_AGGREGATE), rollout_count=1, locations={})
    december = run.ordinary_income_ytd.filter(
        (pl.col("month_index") == 11) & (pl.col("agent_id") == "alice") & (pl.col("ordinary_income_usd") > 0.0)
    ).sort("income_source")

    assert december.get_column("income_source").to_list() == ["interest:corporate", "interest:federal_us"]
    # Row `m` of the YTD frame is the OPENING snapshot of month `m`, so month 11 has months
    # 0..10 in it — eleven payouts, split 60/40. The slices land in their own rows rather than
    # summing into one, which is what makes the per-jurisdiction exemption computable.
    assert december.get_column("ordinary_income_usd").to_list() == [
        pytest.approx(11 * _MONTHLY_PAYOUT_USD * 0.6),
        pytest.approx(11 * _MONTHLY_PAYOUT_USD * 0.4),
    ]


def test_a_tax_character_that_does_not_sum_to_one_is_rejected() -> None:
    """A short split pays out less than the fund distributes, which reads as a lower yield
    rather than as the misconfiguration it is."""

    with pytest.raises(ValueError, match="fractions must sum to 1"):
        _scenario(tax_character=(DistributionTaxSlice(fraction=0.4, issuer_jurisdiction_id="federal_us"),))


def test_a_distribution_on_a_pool_with_no_lots_is_rejected() -> None:
    """Nothing can ever fill it, so the payout would be zero for the whole horizon — which
    looks exactly like a fund that does not distribute."""

    scenario = _scenario(tax_character=_TREASURY)
    scenario = scenario.model_copy(
        update={
            "security_distributions": [
                scenario.security_distributions[0].model_copy(update={"holding_account_id": "ira"})
            ]
        }
    )

    with pytest.raises(ValueError, match="holds no lots"):
        simulate(scenario, rollout_count=1, locations={})


def test_a_declared_distribution_with_no_sampled_payout_series_is_rejected() -> None:
    """Named by the missing series rather than surfacing later as a non-finite payout."""

    scenario = _scenario(tax_character=_TREASURY).model_copy(
        update={
            "external_series": SeriesModelBundle.independent(
                asset_prices=AssetPriceGroups(security={_SYMBOL: Constant(value=_PRICE_USD)})
            )
        }
    )

    with pytest.raises(ValueError, match="security_distribution:bnd"):
        simulate(scenario, rollout_count=1, locations={})


if __name__ == "__main__":
    pytest_bazel.main()
