"""January-start windows over an annual panel, each composed into one `World`.

Annual-only embedding, not a claimed monthly market path: prices and CPI hold within each
year and the year's move lands at month 12, 24, …; the terminal `12 * years` mark carries the
final year's return. Untaxed, every sleeve is a total-return proxy unit, so cash earns its
return in price. Taxed, a unit's price moves by the price return only and the year's income is
paid in its December (month 11, 23, …), inside the tax year that earned it, into the sleeve's
own income account. Checking settles sales and consumption; the tax reserve is the retiree's
cash set aside from withdrawals, outside the portfolio.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

import numpy as np

from finance.augur.model.series import (
    InflationKey,
    LevelSeriesKey,
    SecurityDistributionKey,
    SecurityKey,
    SecuritySymbol,
)
from finance.augur.sim.books import AccountRef
from finance.augur.sim.compiler.execution import compile_series
from finance.augur.sim.compiler.tax import compile_profile
from finance.augur.sim.external_series import ExternalSeriesContext
from finance.augur.sim.fixed_point import (
    currency_amount_to_quanta,
    quantity_to_quanta,
    rate_to_ppb,
    round_currency_amount,
)
from finance.augur.sim.ids import AccountId, AgentId, AssetId, JurisdictionId, LotId
from finance.augur.sim.jurisdictions import JurisdictionLevel
from finance.augur.sim.market_path import MarketPath
from finance.augur.sim.prepared import (
    PreparedAccount,
    PreparedDistribution,
    PreparedDistributionSlice,
    PreparedHoldingPool,
    PreparedJurisdiction,
    PreparedLot,
    PreparedSeries,
)
from finance.augur.sim.runtime import load_jurisdictions_for
from finance.augur.sim.scenario import ORDINARY_INCOME, FilingStatus, InterestIncome, TaxProfile
from finance.augur.sim.tax_authority import TaxAuthority
from finance.augur.sim.world import World
from finance.augur.study.guyton_klinger.panel import PRICED, AnnualPanel, Sleeve

MONTHS_PER_YEAR = 12
# Every sleeve's unit is priced at one dollar in each window's January. Fine enough that
# proxy rounding stays far below any reported digit: micro-dollar money, nano-unit quantities.
QUANTUM = Decimal("0.000001")
QUANTITY_SCALE = 10**9

RETIREE = AgentId("retiree")
WORLD = AgentId("world")
TAX_AUTHORITY = AgentId("tax_authority")
BROKERAGE = AccountId("brokerage")
CHECKING = AccountId("checking")
TAX_RESERVE = AccountId("tax_reserve")
INCOME = {sleeve: AccountId(f"{sleeve}_income") for sleeve in Sleeve}
"""Where each sleeve's payouts land until a review spends or reinvests them."""

FEDERAL = JurisdictionId("federal_us")
CALIFORNIA = JurisdictionId("california")

ADAPTATION_TARGET_PERCENT = {Sleeve.CASH: 10, Sleeve.BONDS: 25, Sleeve.EQUITY: 65}
"""GK2006 Table 1's 65%-equity column with its six equity sleeves merged into one."""


class Taxes(StrEnum):
    """Which investor taxes a window's retiree pays: none (the paper control), or US federal plus California."""

    NONE = "none"
    FEDERAL_CA = "federal-ca"


# Treasury bonds and bills pay interest the federal government issued: federally taxable,
# state-exempt. The S&P dividend has no issuer exemption.
# CLEANUP(added 2026-09-26): declare as qualified dividends once augur/tax-qualified-dividends merges.
PAYOUT_ISSUER: dict[Sleeve, JurisdictionId | None] = {Sleeve.CASH: FEDERAL, Sleeve.BONDS: FEDERAL, Sleeve.EQUITY: None}


@dataclass(frozen=True)
class AnnualWindows:
    """Rollout ID `i` is the window starting in January of `start_years[i]`."""

    start_years: tuple[int, ...]
    years: int
    taxes: Taxes
    series: tuple[PreparedSeries, ...]

    @property
    def horizon_months(self) -> int:
        return self.years * MONTHS_PER_YEAR


def eligible_start_years(panel: AnnualPanel, years: int) -> tuple[int, ...]:
    """Every January start whose `years` complete years the panel covers."""
    if years < 1:
        raise ValueError(f"{years=} must be positive")
    return tuple(panel.years[: max(0, len(panel.years) - years + 1)])


def _growth(returns: Sequence[float], start_index: int, years: int) -> np.ndarray:
    """The unit's level at each January, `years + 1` of them."""
    return np.cumprod([1.0, *(1.0 + np.asarray(returns[start_index : start_index + years]))])


def _monthly(growth: np.ndarray) -> np.ndarray:
    return np.repeat(growth, [*([MONTHS_PER_YEAR] * (len(growth) - 1)), 1])


def _payouts(growth: np.ndarray, income: Sequence[float], start_index: int) -> np.ndarray:
    """Per-unit payout: each December, the year's income at the price held through it."""
    years = len(growth) - 1
    payouts = np.zeros(years * MONTHS_PER_YEAR + 1)
    payouts[MONTHS_PER_YEAR - 1 :: MONTHS_PER_YEAR] = growth[:-1] * np.asarray(
        income[start_index : start_index + years]
    )
    return payouts


def annual_windows(panel: AnnualPanel, *, start_years: Sequence[int], years: int, taxes: Taxes) -> AnnualWindows:
    """Materialize the selected windows once; no imputation, every window complete."""
    if not start_years or len(set(start_years)) != len(start_years):
        raise ValueError(f"{start_years=} must be nonempty and distinct")
    eligible = eligible_start_years(panel, years)
    missing = [year for year in start_years if year not in eligible]
    if missing:
        raise ValueError(f"panel {panel.years} lacks {years} complete years from {missing}")
    offsets = [year - panel.first_year for year in start_years]
    blocks: list[tuple[LevelSeriesKey, np.ndarray]] = [
        (InflationKey(), np.stack([_monthly(_growth(panel.inflation, offset, years)) for offset in offsets]))
    ]
    for sleeve in Sleeve:
        symbol = SecuritySymbol(sleeve)
        if taxes is Taxes.NONE:
            prices = [_growth(panel.total_return(sleeve), offset, years) for offset in offsets]
        else:
            prices = [
                _growth(panel.price[sleeve], offset, years) if sleeve in PRICED else np.ones(years + 1)
                for offset in offsets
            ]
            blocks.append(
                (
                    SecurityDistributionKey(symbol=symbol),
                    np.stack(
                        [
                            _payouts(growth, panel.income[sleeve], offset)
                            for growth, offset in zip(prices, offsets, strict=True)
                        ]
                    ),
                )
            )
        blocks.append((SecurityKey(symbol=symbol), np.stack([_monthly(growth) for growth in prices])))
    horizon = years * MONTHS_PER_YEAR
    return AnnualWindows(
        start_years=tuple(start_years),
        years=years,
        taxes=taxes,
        series=compile_series(
            ExternalSeriesContext.from_level_blocks(blocks, rollout_count=len(offsets), horizon_months=horizon),
            rollout_count=len(offsets),
            horizon_months=horizon,
            currency_quantum=QUANTUM,
        ),
    )


def _declare_taxes(world: World) -> None:
    """A single California resident's federal and state tax, with sleeve payouts characterized by issuer.

    The bundled tables hold fixed in nominal dollars. No prior-year tax: no estimated
    instalments, so each year's whole liability falls due at the next January review.
    """
    # TODO: index brackets and thresholds to the window's CPI; fixed, early start years pay
    #   2024 nominal thresholds and inflation pushes constant real income into higher brackets.
    # TODO: estimated instalments sized from each actual prior year, once the tax authority can
    #   size them per year; one fixed prior-year tax would be wrong in nearly every year.
    # TODO: declare the SALT deduction; California tax is never itemized federally.
    profile = TaxProfile(
        agent_id=RETIREE,
        filing_status=FilingStatus.SINGLE,
        jurisdiction_ids=[FEDERAL, CALIFORNIA],
        tax_authority_agent_id=TAX_AUTHORITY,
        payment_account_id=TAX_RESERVE,
        tax_authority_account_id=CHECKING,
    )
    world.declare_account(
        PreparedAccount(account=AccountRef(agent_id=TAX_AUTHORITY, account_id=CHECKING), opening_balance=0)
    )
    world.track(TaxAuthority(compile_profile(profile, load_jurisdictions_for([profile]), quantum=QUANTUM)))
    for sleeve in Sleeve:
        world.declare_distribution(
            PreparedDistribution(
                agent_id=RETIREE,
                holding_account_id=BROKERAGE,
                asset_id=AssetId(sleeve),
                to_account_id=INCOME[sleeve],
                tax_character=(
                    PreparedDistributionSlice(
                        fraction_ppb=rate_to_ppb(1.0), issuer_jurisdiction_id=PAYOUT_ISSUER[sleeve]
                    ),
                ),
            )
        )


def compose_world(windows: AnnualWindows, rollout_id: int, *, wealth: Decimal, weights: Mapping[Sleeve, int]) -> World:
    """One window's books: `wealth` split by `weights` into sleeve lots, empty cash accounts.

    Every sleeve's pool is declared, held or not. An untaxed window declares no tax
    vocabulary: the paper control has no investor taxes or trading fees.
    """
    # TODO: fund expense ratios and trading fees, taxed or not.
    total = sum(weights.values())
    if set(weights) != set(Sleeve) or min(weights.values()) < 0 or total <= 0:
        raise ValueError(f"{weights=} must weigh every sleeve, nonnegatively, with a positive total")
    taxed = windows.taxes is Taxes.FEDERAL_CA
    world = World(
        MarketPath(windows.series, rollout_id, rollout_count=len(windows.start_years)),
        horizon_months=windows.horizon_months,
        income_sources=(ORDINARY_INCOME, *(InterestIncome(issuer_jurisdiction_id=i) for i in (None, FEDERAL)))
        if taxed
        else (),
        jurisdictions=(
            PreparedJurisdiction(jurisdiction_id=FEDERAL, level=JurisdictionLevel.FEDERAL),
            PreparedJurisdiction(jurisdiction_id=CALIFORNIA, level=JurisdictionLevel.STATE),
        )
        if taxed
        else (),
    )
    retiree_accounts = [CHECKING, *([TAX_RESERVE, *INCOME.values()] if taxed else [])]
    for account in (
        *(AccountRef(agent_id=RETIREE, account_id=account_id) for account_id in retiree_accounts),
        AccountRef(agent_id=WORLD, account_id=CHECKING),
    ):
        world.declare_account(PreparedAccount(account=account, opening_balance=0))
    for sleeve in Sleeve:
        world.declare_pool(
            PreparedHoldingPool(
                agent_id=RETIREE, account_id=BROKERAGE, asset_id=AssetId(sleeve), quantity_scale=QUANTITY_SCALE
            )
        )
    if taxed:
        _declare_taxes(world)
    for sleeve in Sleeve:
        if not weights[sleeve]:
            continue
        value = round_currency_amount(wealth * weights[sleeve] / total, quantum=QUANTUM)
        world.hold(
            PreparedLot(
                lot_id=LotId(f"{sleeve}_opening"),
                agent_id=RETIREE,
                account_id=BROKERAGE,
                asset_id=AssetId(sleeve),
                purchase_month=-1,
                quantity_scale=QUANTITY_SCALE,
                units=int(quantity_to_quanta(value, scale=QUANTITY_SCALE)),
                basis=int(currency_amount_to_quanta(value, quantum=QUANTUM)),
            )
        )
    return world
