"""The IRC 1211(b) cap is a taxpayer's figure, and the compiler will not guess it.

The netting runs once per taxpayer and its result feeds every jurisdiction that taxpayer
files in, so a profile whose jurisdictions cap the ordinary offset differently has no single
answer. Most states conform to the federal $3,000 and the two shipped here do, which is
exactly why this needs saying: while they agree, either level could be read as the source of
truth, and nothing would notice a jurisdiction that stopped conforming.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
import pytest_bazel

from finance.augur.sim.compiler.execution import prepare_run
from finance.augur.sim.compiler.tax import compile_tax
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.jurisdictions import Jurisdiction, JurisdictionLevel, TaxBracket, load_jurisdiction
from finance.augur.sim.scenario import Agent, Currency, InitialAccountBalance, OrdinaryIncome, Scenario, TaxProfile


def _scenario(*jurisdiction_ids: str) -> Scenario:
    return Scenario(
        agents=[Agent(agent_id="alice"), Agent(agent_id="irs")],
        initial_cash=[
            InitialAccountBalance(agent_id=agent_id, account_id="checking", balance=Decimal(0))
            for agent_id in ("alice", "irs")
        ],
        tax_profiles=[
            TaxProfile(agent_id="alice", jurisdiction_ids=list(jurisdiction_ids), tax_authority_agent_id="irs")
        ],
        horizon_months=13,
    )


def _capping(jurisdiction: Jurisdiction, *, offset: Decimal) -> Jurisdiction:
    return jurisdiction.model_copy(update={"max_capital_loss_ordinary_offset": {"single": offset}})


def _compile(scenario: Scenario, jurisdictions: dict[str, Jurisdiction]) -> None:
    prepare_run(
        scenario,
        rollout_count=1,
        external_series=ExternalSeriesContext.from_level_blocks(
            [], rollout_count=1, horizon_months=int(scenario.horizon_months)
        ),
        jurisdictions=jurisdictions,
        locations={},
    )


def test_the_shipped_jurisdictions_agree_on_the_cap() -> None:
    """The premise of the rejection below: today's data compiles, so failing means disagreement."""

    jurisdictions = {name: load_jurisdiction(name) for name in ("federal_us", "california")}
    _compile(_scenario("federal_us", "california"), jurisdictions)


def test_a_profile_whose_jurisdictions_cap_the_offset_differently_is_refused() -> None:
    """One netting per taxpayer cannot answer for two caps, so the compiler says so.

    Refusing beats picking: silently taking one jurisdiction's rule reports numbers for the
    other that its own law does not support, and nothing downstream could tell.
    """

    federal = load_jurisdiction("federal_us")
    california = _capping(load_jurisdiction("california"), offset=Decimal(0))
    with pytest.raises(ValueError, match="cap the capital-loss ordinary offset differently"):
        _compile(_scenario("federal_us", "california"), {"federal_us": federal, "california": california})


def test_a_single_jurisdiction_may_cap_the_offset_at_anything() -> None:
    """Nothing to disagree with, so a state that allows no offset at all still compiles."""

    _compile(_scenario("california"), {"california": _capping(load_jurisdiction("california"), offset=Decimal(0))})


def test_profile_order_routes_and_jurisdiction_specific_rules_survive_preparation() -> None:
    scenario = _scenario("california", "federal_us")
    scenario.agents.append(Agent(agent_id="bob"))
    scenario.initial_cash.append(InitialAccountBalance(agent_id="bob", account_id="tax-cash", balance=Decimal(0)))
    scenario.tax_profiles.append(
        TaxProfile(
            agent_id="bob",
            jurisdiction_ids=["federal_us"],
            tax_authority_agent_id="irs",
            payment_account_id="tax-cash",
            prior_year_tax=Decimal("123.45"),
        )
    )
    prepared = compile_tax(scenario, {name: load_jurisdiction(name) for name in ("federal_us", "california")})
    alice, bob = prepared.profiles
    assert [alice.agent_id, bob.agent_id] == ["alice", "bob"]
    assert [rule.jurisdiction_id for rule in alice.jurisdictions] == ["california", "federal_us"]
    assert [rule.jurisdiction_id for rule in bob.jurisdictions] == ["federal_us"]
    assert (bob.payment_account_id, bob.tax_authority_agent_id, bob.tax_authority_account_id) == (
        "tax-cash",
        "irs",
        "checking",
    )
    assert bob.prior_year_tax == 12_345
    assert alice.section_121_exclusion == bob.section_121_exclusion == 25_000_000
    california, federal = alice.jurisdictions
    assert california.exempt_interest_from_levels == (JurisdictionLevel.FEDERAL,)
    assert california.exempts_own_issue
    assert federal.exempt_interest_from_levels == (JurisdictionLevel.STATE,)
    assert not federal.exempts_own_issue
    assert california.section_1250_rate_ppb == 0
    assert federal.section_1250_rate_ppb == 250_000_000
    assert california.long_term_capital_gain_brackets == ()
    assert [bracket.rate_ppb for bracket in federal.long_term_capital_gain_brackets] == [0, 150_000_000, 200_000_000]


def test_prepared_thresholds_use_exact_quantum_and_rates_keep_half_away_rounding() -> None:
    scenario = _scenario("federal_us")
    scenario.currency = Currency(code="USD", quantum=Decimal("0.05"))
    jurisdiction = load_jurisdiction("federal_us").model_copy(
        update={
            "ordinary_income_brackets": {
                "single": [
                    TaxBracket(upper=Decimal("10.05"), rate=0.1000000005),
                    TaxBracket(upper="Infinity", rate=0.20),
                ]
            },
            "standard_deduction": {"single": Decimal("5.05")},
        }
    )
    [profile] = compile_tax(scenario, {"federal_us": jurisdiction}).profiles
    [rule] = profile.jurisdictions
    first, last = rule.ordinary_brackets
    assert (first.upper, first.rate_ppb) == (201, 100_000_001)
    assert (last.upper, last.rate_ppb) == (None, 200_000_000)
    assert rule.standard_deduction == 101
    assert rule.max_capital_loss_ordinary_offset == 60_000
    assert profile.section_121_exclusion == 5_000_000
    # Prepared schedules own the resolved values, not references to mutable source tables.
    jurisdiction.ordinary_income_brackets["single"][0].upper = Decimal("99.95")
    assert first.upper == 201


def test_largest_finite_threshold_is_not_an_open_bracket_sentinel() -> None:
    scenario = _scenario("federal_us")
    maximum = (1 << 63) - 1
    jurisdiction = load_jurisdiction("federal_us").model_copy(
        update={
            "ordinary_income_brackets": {
                "single": [
                    TaxBracket(upper=Decimal(maximum) * scenario.currency.quantum, rate=0.10),
                    TaxBracket(upper="Infinity", rate=0.20),
                ]
            }
        }
    )
    [profile] = compile_tax(scenario, {"federal_us": jurisdiction}).profiles
    assert [bracket.upper for bracket in profile.jurisdictions[0].ordinary_brackets] == [maximum, None]


def test_no_taxpayers_still_declares_ordinary_income_without_phantom_rules() -> None:
    scenario = _scenario("federal_us")
    scenario.tax_profiles = []
    prepared = compile_tax(scenario, {})
    assert prepared.profiles == ()
    assert prepared.income_sources == (OrdinaryIncome(),)
    _compile(scenario, {})


if __name__ == "__main__":
    pytest_bazel.main()
