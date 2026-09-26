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

from finance.augur.sim.compiler.tax import compile_income_sources, compile_profile
from finance.augur.sim.ids import AccountId, AgentId, JurisdictionId
from finance.augur.sim.jurisdictions import Jurisdiction, JurisdictionLevel, TaxBracket, load_jurisdiction
from finance.augur.sim.scenario import OrdinaryIncome, TaxProfile

FEDERAL_US = JurisdictionId("federal_us")

CENT = Decimal("0.01")


def _alice(*jurisdiction_ids: JurisdictionId) -> TaxProfile:
    return TaxProfile(
        agent_id=AgentId("alice"), jurisdiction_ids=list(jurisdiction_ids), tax_authority_agent_id=AgentId("irs")
    )


def _capping(jurisdiction: Jurisdiction, *, offset: Decimal) -> Jurisdiction:
    return jurisdiction.model_copy(update={"max_capital_loss_ordinary_offset": {"single": offset}})


def test_the_shipped_jurisdictions_agree_on_the_cap() -> None:
    """The premise of the rejection below: today's data compiles, so failing means disagreement."""

    jurisdictions = {name: load_jurisdiction(name) for name in (FEDERAL_US, JurisdictionId("california"))}
    compile_profile(_alice(FEDERAL_US, JurisdictionId("california")), jurisdictions, quantum=CENT)


def test_a_profile_whose_jurisdictions_cap_the_offset_differently_is_refused() -> None:
    """One netting per taxpayer cannot answer for two caps, so the compiler says so.

    Refusing beats picking: silently taking one jurisdiction's rule reports numbers for the
    other that its own law does not support, and nothing downstream could tell.
    """

    federal = load_jurisdiction(FEDERAL_US)
    california = _capping(load_jurisdiction(JurisdictionId("california")), offset=Decimal(0))
    with pytest.raises(ValueError, match="cap the capital-loss ordinary offset differently"):
        compile_profile(
            _alice(FEDERAL_US, JurisdictionId("california")),
            {FEDERAL_US: federal, JurisdictionId("california"): california},
            quantum=CENT,
        )


def test_a_single_jurisdiction_may_cap_the_offset_at_anything() -> None:
    """Nothing to disagree with, so a state that allows no offset at all still compiles."""

    compile_profile(
        _alice(JurisdictionId("california")),
        {JurisdictionId("california"): _capping(load_jurisdiction(JurisdictionId("california")), offset=Decimal(0))},
        quantum=CENT,
    )


def test_profile_order_routes_and_jurisdiction_specific_rules_survive_preparation() -> None:
    jurisdictions = {name: load_jurisdiction(name) for name in (FEDERAL_US, JurisdictionId("california"))}
    alice, bob = (
        compile_profile(profile, jurisdictions, quantum=CENT)
        for profile in (
            _alice(JurisdictionId("california"), FEDERAL_US),
            TaxProfile(
                agent_id=AgentId("bob"),
                jurisdiction_ids=[FEDERAL_US],
                tax_authority_agent_id=AgentId("irs"),
                payment_account_id=AccountId("tax-cash"),
                prior_year_tax=Decimal("123.45"),
            ),
        )
    )
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
    jurisdiction = load_jurisdiction(FEDERAL_US).model_copy(
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
    profile = compile_profile(_alice(FEDERAL_US), {FEDERAL_US: jurisdiction}, quantum=Decimal("0.05"))
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
    maximum = (1 << 63) - 1
    jurisdiction = load_jurisdiction(FEDERAL_US).model_copy(
        update={
            "ordinary_income_brackets": {
                "single": [
                    TaxBracket(upper=Decimal(maximum) * CENT, rate=0.10),
                    TaxBracket(upper="Infinity", rate=0.20),
                ]
            }
        }
    )
    profile = compile_profile(_alice(FEDERAL_US), {FEDERAL_US: jurisdiction}, quantum=CENT)
    assert [bracket.upper for bracket in profile.jurisdictions[0].ordinary_brackets] == [maximum, None]


def test_nothing_named_still_declares_ordinary_income() -> None:
    assert compile_income_sources(flows=(), bonds=(), distributions=()) == (OrdinaryIncome(),)


if __name__ == "__main__":
    pytest_bazel.main()
