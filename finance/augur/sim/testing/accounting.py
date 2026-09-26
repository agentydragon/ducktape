"""Small exact financial books, and a world over them, for transaction admission and annual-close controls."""

from collections.abc import Mapping, Sequence

from finance.augur.sim.accounting import Accounting
from finance.augur.sim.books import AccountRef
from finance.augur.sim.compiler.tax import PreparedTaxBracket, PreparedTaxProfile, PreparedTaxRules
from finance.augur.sim.ids import AccountId, AgentId, JurisdictionId
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import PreparedAccount, PreparedSeries
from finance.augur.sim.scenario import ORDINARY_INCOME, InterestIncome
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.world import World

HOUSEHOLD = AgentId("test_household")
OTHER = AgentId("test_other")
WORLD = AgentId("test_world")
CASH = AccountRef(agent_id=HOUSEHOLD, account_id=AccountId("checking"))
RESERVE = AccountRef(agent_id=HOUSEHOLD, account_id=AccountId("savings"))
RECIPIENT = AccountRef(agent_id=OTHER, account_id=AccountId("checking"))
EXOGENOUS = AccountRef(agent_id=WORLD, account_id=AccountId("cash"))
INCOME_SOURCES = (ORDINARY_INCOME, InterestIncome())


def flat_rules(jurisdiction: JurisdictionId, rate: int) -> PreparedTaxRules:
    return PreparedTaxRules(jurisdiction, (), False, (PreparedTaxBracket(None, rate),), (), 0, 300_000, 0)


def taxpayer(agent_id: AgentId) -> PreparedTaxProfile:
    """A flat 10% single-jurisdiction filer who pays the world from `checking`."""
    return PreparedTaxProfile(
        agent_id=agent_id,
        tax_authority_agent_id=WORLD,
        payment_account_id=AccountId("checking"),
        tax_authority_account_id=AccountId("cash"),
        prior_year_tax=0,
        section_121_exclusion=0,
        jurisdictions=(flat_rules(JurisdictionId("test_federal"), 100_000_000),),
    )


def opening(balances: Mapping[AccountRef, int]) -> tuple[PreparedAccount, ...]:
    """The four accounts, each opening at its balance here and at zero otherwise."""
    return tuple(
        PreparedAccount(account=account, opening_balance=balances.get(account, 0))
        for account in (CASH, RESERVE, RECIPIENT, EXOGENOUS)
    )


ACCOUNTS = opening({CASH: 100})
TAXPAYERS = (taxpayer(HOUSEHOLD), taxpayer(OTHER))


def accounting(
    accounts: Sequence[PreparedAccount] = ACCOUNTS, taxpayers: Sequence[PreparedTaxProfile] = TAXPAYERS
) -> Accounting:
    """The accounts and taxpayers on a fresh ledger."""
    books = Accounting(INCOME_SOURCES)
    for account in accounts:
        books.declare(account)
    for profile in taxpayers:
        books.enroll(profile)
    return books


def world_on(
    series: Sequence[PreparedSeries],
    *,
    horizon_months: int,
    rollout_id: int = 0,
    rollout_count: int = 1,
    accounts: Sequence[PreparedAccount] = ACCOUNTS,
    taxpayers: Sequence[PreparedTaxProfile] = TAXPAYERS,
) -> World:
    """An unstarted world on one path of `series` with the accounts declared and each taxpayer's authority tracked."""
    world = World(
        MarketPath(series, rollout_id, rollout_count=rollout_count),
        horizon_months=horizon_months,
        income_sources=INCOME_SOURCES,
    )
    for account in accounts:
        world.declare_account(account)
    for profile in taxpayers:
        world.track(TaxAuthority(profile))
    return world
