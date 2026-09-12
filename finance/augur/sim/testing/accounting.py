"""Small exact financial books for transaction admission and annual-close controls."""

from dataclasses import replace
from decimal import Decimal

from finance.augur.sim.accounting import Accounting
from finance.augur.sim.books import AccountRef
from finance.augur.sim.compiler.tax import PreparedTaxBracket, PreparedTaxProfile, PreparedTaxRules
from finance.augur.sim.ids import AgentId
from finance.augur.sim.prepared import PreparedScenario
from finance.augur.sim.scenario import ORDINARY_INCOME, InitialAccountBalance, InterestIncome
from finance.augur.sim.testing.case import Case, scenario

HOUSEHOLD = AgentId("test_household")
OTHER = AgentId("test_other")
WORLD = AgentId("test_world")
CASH = AccountRef(agent_id=HOUSEHOLD, account_id="checking")
RESERVE = AccountRef(agent_id=HOUSEHOLD, account_id="savings")
RECIPIENT = AccountRef(agent_id=OTHER, account_id="checking")
EXOGENOUS = AccountRef(agent_id=WORLD, account_id="cash")


def flat_rules(jurisdiction: str, rate: int) -> PreparedTaxRules:
    return PreparedTaxRules(jurisdiction, (), False, (PreparedTaxBracket(None, rate),), (), 0, 300_000, 0)


def prepared_scenario() -> PreparedScenario:
    authored = scenario(
        [
            InitialAccountBalance(
                agent_id=account.agent_id,
                account_id=account.account_id,
                balance=Decimal("1.00") if account == CASH else Decimal(0),
            )
            for account in (CASH, RESERVE, RECIPIENT, EXOGENOUS)
        ],
        horizon_months=25,
        tax_profiles=[],
    )
    prepared = Case(authored, rollout_count=1).compiled_run.scenario
    profiles = tuple(
        PreparedTaxProfile(
            agent_id=agent,
            tax_authority_agent_id=WORLD,
            payment_account_id="checking",
            tax_authority_account_id="cash",
            prior_year_tax=0,
            section_121_exclusion=0,
            jurisdictions=(flat_rules("test_federal", 100_000_000),),
        )
        for agent in (HOUSEHOLD, OTHER)
    )
    return replace(prepared, tax_profiles=profiles, income_sources=(ORDINARY_INCOME, InterestIncome()))


def prepared_books(scenario_: PreparedScenario) -> Accounting:
    """The prepared scenario's accounts and taxpayers on a fresh ledger."""
    accounting_ = Accounting(scenario_.income_sources, scenario_.jurisdictions)
    for account in scenario_.accounts:
        accounting_.declare(account)
    for profile in scenario_.tax_profiles:
        accounting_.enroll(profile)
    return accounting_


def accounting() -> Accounting:
    return prepared_books(prepared_scenario())
