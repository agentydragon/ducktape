"""Imported prepared contracts fail before any mutable rollout is constructed."""

from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch

import pytest
import pytest_bazel

from finance.augur.sim.books import AccountRef
from finance.augur.sim.prepared import (
    CompiledRun,
    PreparedBond,
    PreparedDistribution,
    PreparedDistributionSlice,
    PreparedFixedAmount,
    PreparedHoldingPool,
    PreparedIndexedCoupon,
    PreparedLocation,
    PreparedLot,
    PreparedSeries,
    PreparedTransfer,
    _PropertyPurchase,
)
from finance.augur.sim.scenario import InterestIncome
from finance.augur.sim.session import _Session
from finance.augur.sim.testing.accounting import CASH, EXOGENOUS, HOUSEHOLD, WORLD, prepared_scenario
from finance.augur.sim.validation import validate


@pytest.fixture
def run() -> CompiledRun:
    return CompiledRun(
        currency_code="USD",
        currency_quantum="0.01",
        rollout_count=1,
        scenario=replace(prepared_scenario(), horizon_months=1, tax_profiles=()),
        series=(),
    )


def rejects_before_world(run: CompiledRun, match: str) -> None:
    before = deepcopy(run)
    with patch("finance.augur.sim.session.World") as world:
        with pytest.raises(ValueError, match=match):
            _Session(run, None, [0], capture="forensic", configured=True)
        world.assert_not_called()
    assert run == before


@pytest.mark.parametrize("invalid", ["rollouts", "horizon", "currency", "quantum"])
def test_rejects_invalid_fixture_metadata(run: CompiledRun, invalid: str) -> None:
    validate(run)
    if invalid == "rollouts":
        run = replace(run, rollout_count=0)
        match = "rollout"
    elif invalid == "horizon":
        run = replace(run, scenario=replace(run.scenario, horizon_months=0))
        match = "horizon"
    elif invalid == "currency":
        run = replace(run, currency_code="usd")
        match = "currency"
    else:
        run = replace(run, currency_quantum="0")
        match = "quantum"
    rejects_before_world(run, match)


def test_rejects_invalid_references_before_rollout_execution(run: CompiledRun) -> None:
    transfer = PreparedTransfer(
        month=0,
        cause_id="test-transfer",
        from_account=EXOGENOUS,
        to_account=CASH,
        amount=1,
        income_category=None,
        deduction_category=None,
    )
    valid = replace(run, scenario=replace(run.scenario, scheduled_transfers=(transfer,)))
    validate(valid)
    invalid = replace(transfer, from_account=AccountRef(agent_id="missing", account_id="checking"))
    rejects_before_world(
        replace(valid, scenario=replace(valid.scenario, scheduled_transfers=(invalid,))), "unknown account"
    )


def test_rejects_income_from_a_source_the_scenario_did_not_declare(run: CompiledRun) -> None:
    transfer = PreparedTransfer(
        month=0,
        cause_id="test-interest",
        from_account=EXOGENOUS,
        to_account=CASH,
        amount=1,
        income_category=InterestIncome(issuer_jurisdiction_id="test-state"),
        deduction_category=None,
    )
    invalid = replace(run, scenario=replace(run.scenario, scheduled_transfers=(transfer,)))
    rejects_before_world(invalid, "undeclared income")
    validate(
        replace(
            invalid,
            scenario=replace(
                invalid.scenario,
                income_sources=(*invalid.scenario.income_sources, InterestIncome(issuer_jurisdiction_id="test-state")),
            ),
        )
    )


@pytest.fixture
def public(run: CompiledRun) -> CompiledRun:
    lot = PreparedLot(
        lot_id="test-lot",
        agent_id=HOUSEHOLD,
        account_id="brokerage",
        asset_id="stock",
        purchase_month=-2,
        quantity_scale=1000,
        units=1000,
        basis=100,
    )
    return replace(
        run,
        scenario=replace(
            run.scenario,
            initial_lots=(lot,),
            holding_pools=(
                PreparedHoldingPool(agent_id=HOUSEHOLD, account_id="brokerage", asset_id="stock", quantity_scale=1000),
            ),
        ),
        series=(PreparedSeries(series_id="security:stock", snapshots=2, values=(100, 100)),),
    )


@pytest.mark.parametrize("invalid", ["incomplete", "unknown-issuer"])
def test_distribution_tax_character_requires_a_complete_known_issuer_split(public: CompiledRun, invalid: str) -> None:
    slice_ = PreparedDistributionSlice(fraction_ppb=1_000_000_000, issuer_jurisdiction_id=None)
    distribution = PreparedDistribution(
        agent_id=HOUSEHOLD,
        holding_account_id="brokerage",
        asset_id="stock",
        to_account_id="checking",
        tax_character=(slice_,),
    )
    valid = replace(
        public,
        scenario=replace(public.scenario, distributions=(distribution,)),
        series=(*public.series, PreparedSeries(series_id="security_distribution:stock", snapshots=2, values=(1, 1))),
    )
    validate(valid)
    bad = (
        replace(slice_, fraction_ppb=400_000_000)
        if invalid == "incomplete"
        else replace(slice_, issuer_jurisdiction_id="unknown")
    )
    rejects_before_world(
        replace(valid, scenario=replace(valid.scenario, distributions=(replace(distribution, tax_character=(bad,)),))),
        "tax character" if invalid == "incomplete" else "unknown.*issuer",
    )


@pytest.mark.parametrize("invalid", ["mixed-scales", "negative-price"])
def test_rejects_mixed_quantity_scales_and_invalid_security_prices(public: CompiledRun, invalid: str) -> None:
    validate(public)
    if invalid == "mixed-scales":
        second = replace(public.scenario.initial_lots[0], lot_id="other-lot", quantity_scale=10, units=10)
        public = replace(
            public, scenario=replace(public.scenario, initial_lots=(*public.scenario.initial_lots, second))
        )
        match = "quantity scale"
    else:
        public = replace(public, series=(replace(public.series[0], values=(100, -1)),))
        match = "non-positive"
    rejects_before_world(public, match)


def test_zero_distribution_is_valid_but_negative_distribution_and_zero_price_are_not(run: CompiledRun) -> None:
    zero = PreparedSeries(series_id="security_distribution:test", snapshots=2, values=(0, 0))
    validate(replace(run, series=(zero,)))
    session = _Session(replace(run, series=(zero,)), None, [0], capture="forensic", configured=True)
    session.start()
    session.close_month()
    assert session.is_finished()
    session.close()
    rejects_before_world(replace(run, series=(replace(zero, values=(0, -1)),)), "negative security distribution")
    rejects_before_world(
        replace(run, series=(replace(zero, series_id="security:test", values=(100, 0)),)), "non-positive"
    )


@pytest.mark.parametrize("invalid", ["non-par", "negative-coupon", "missing-index", "unknown-issuer"])
def test_bond_validation_rejects_non_par_and_missing_index_paths(run: CompiledRun, invalid: str) -> None:
    bond = PreparedBond(
        bond_id="test-bond",
        agent_id=HOUSEHOLD,
        account_id="checking",
        issuer_jurisdiction_id=None,
        face_value=100,
        purchase_price=100,
        coupon=PreparedFixedAmount(amount=3),
        coupon_period_months=6,
        purchase_month_index=-6,
        maturity_month_index=6,
    )
    validate(replace(run, scenario=replace(run.scenario, initial_bonds=(bond,))))
    if invalid == "non-par":
        bond = replace(bond, purchase_price=99)
        match = "bond terms"
    elif invalid == "negative-coupon":
        bond = replace(bond, coupon=PreparedFixedAmount(amount=-1))
        match = "bond terms"
    elif invalid == "missing-index":
        bond = replace(bond, coupon=PreparedIndexedCoupon(annual_rate_ppb=50_000_000))
        match = "inflation"
    else:
        bond = replace(bond, issuer_jurisdiction_id="unknown")
        match = "unknown.*issuer"
    rejects_before_world(replace(run, scenario=replace(run.scenario, initial_bonds=(bond,))), match)


@pytest.mark.parametrize("invalid", ["unknown-location", "unfinanced-gap"])
def test_rejects_invalid_property_contracts_before_rollout_execution(run: CompiledRun, invalid: str) -> None:
    location = PreparedLocation(
        location_id="test-market",
        display_name="Test market",
        jurisdiction_ids=(),
        annual_property_tax_rate_ppb=0,
        annual_special_assessment=0,
    )
    purchase = _PropertyPurchase(
        month=0,
        cause_id="test-purchase",
        property_id="test-home",
        location_id="test-market",
        buyer_agent_id=HOUSEHOLD,
        buyer_account_id="checking",
        seller_agent_id=WORLD,
        seller_account_id="cash",
        purchase_price=10,
        down_payment=10,
        buyer_closing_cost=0,
        rented_fraction_ppb=0,
        land_value_fraction_ppb=200_000_000,
        mortgage=None,
    )
    valid = replace(
        run, scenario=replace(run.scenario, locations=(location,), _scheduled_property_purchases=(purchase,))
    )
    validate(valid)
    if invalid == "unknown-location":
        valid = replace(valid, scenario=replace(valid.scenario, locations=()))
        match = "unknown location"
    else:
        valid = replace(
            valid, scenario=replace(valid.scenario, _scheduled_property_purchases=(replace(purchase, down_payment=9),))
        )
        match = "property terms"
    rejects_before_world(valid, match)


if __name__ == "__main__":
    pytest_bazel.main()
