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

from finance.augur.sim.compiler.plan import compile_simulation
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.jurisdictions import Jurisdiction, load_jurisdiction
from finance.augur.sim.scenario import Agent, InitialAccountBalance, Scenario, TaxProfile


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
    compile_simulation(
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


if __name__ == "__main__":
    pytest_bazel.main()
