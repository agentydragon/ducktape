"""The deployment's opening positions as the prepared facts a world declares.

The records here are the app's own: each is what one path's world is told, in quanta.
`scenarios.compose` declares them onto that world.

`opening_holdings` checks them once, when the service starts. Money becomes quanta per
request, in the request's currency; everything else is prepared once.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from finance.augur.api.config import SecurityDistributionConfig
from finance.augur.api.portfolio import (
    BondHoldingConfig,
    HoldingPositionConfig,
    HoldingTaxLotConfig,
    PortfolioConfig,
    TlhPortfolioSpec,
)
from finance.augur.model.asset_key import AssetKey, PrivateEquityAssetKey
from finance.augur.model.series import SecurityKey, SecuritySymbol
from finance.augur.sim.bonds import coupon_amount_quanta
from finance.augur.sim.fixed_point import quantity_scale_for_asset, round_ppb
from finance.augur.sim.ids import AccountId, AgentId, AssetId, BondId, LotId, PortfolioId
from finance.augur.sim.income import InterestCharacter, InterestIncome, TransferIncomeCategory
from finance.augur.sim.money import Currency
from finance.augur.sim.observations import FixedCoupon, IndexedCoupon
from finance.augur.sim.tlh import TlhAssumptions, TlhOpeningCohort


@dataclass(frozen=True, kw_only=True)
class Pool:
    agent_id: AgentId
    account_id: AccountId
    asset_id: AssetId
    quantity_scale: int


@dataclass(frozen=True, kw_only=True)
class Lot:
    lot_id: LotId
    agent_id: AgentId
    account_id: AccountId
    asset_id: AssetId
    purchase_month: int
    quantity_scale: int
    units: int
    basis: int


@dataclass(frozen=True, kw_only=True)
class Bond:
    bond_id: BondId
    agent_id: AgentId
    account_id: AccountId
    character: InterestCharacter
    face_value: int
    purchase_price: int
    coupon: FixedCoupon | IndexedCoupon
    coupon_period_months: int
    purchase_month_index: int
    maturity_month_index: int


@dataclass(frozen=True, kw_only=True)
class ManagedPortfolio:
    portfolio_id: PortfolioId
    owner_agent_id: AgentId
    account_id: AccountId
    asset_id: AssetId
    initial_cohorts: tuple[TlhOpeningCohort, ...]
    assumptions: TlhAssumptions


@dataclass(frozen=True, kw_only=True)
class Distribution:
    agent_id: AgentId
    holding_account_id: AccountId
    asset_id: AssetId
    to_account_id: AccountId
    tax_character: Mapping[TransferIncomeCategory, int]


@dataclass(frozen=True)
class Holdings:
    """The primary agent's opening positions, checked; what needs no currency is already prepared."""

    portfolio: PortfolioConfig
    tlh_portfolios: tuple[TlhPortfolioSpec, ...]
    distributions: tuple[Distribution, ...]


def opening_holdings(
    portfolio: PortfolioConfig,
    declarations: tuple[SecurityDistributionConfig, ...],
    *,
    tlh_portfolios: tuple[TlhPortfolioSpec, ...],
    primary_agent_id: AgentId,
    payout_account_id: AccountId,
) -> Holdings:
    """The portfolio's positions, refusing any the app cannot project: another agent's, or a bond it cannot value.

    `payout_account_id` is where distributions land. Portfolio accounts are custody
    accounts and carry no cash row, so a payout into one would have nowhere to go.
    """

    owners = _account_owners(portfolio)
    unsupported_owner_ids = sorted(
        {owners[position.account_id] for position in portfolio.holdings} - {primary_agent_id}
    )
    if unsupported_owner_ids:
        raise ValueError(
            "product portfolio projection only supports holding lots owned by the primary agent; "
            f"got owner agent ids {unsupported_owner_ids}"
        )
    for bond in portfolio.bonds:
        _check_bond_terms(bond)
    unsupported_owner_ids = sorted({owners[bond.account_id] for bond in portfolio.bonds} - {primary_agent_id})
    if unsupported_owner_ids:
        raise ValueError(
            "product portfolio projection only supports bonds owned by the primary agent; "
            f"got owner agent ids {unsupported_owner_ids}"
        )
    distributions = _distributions(
        portfolio, declarations, tlh_portfolios=tlh_portfolios, payout_account_id=payout_account_id
    )
    unsupported_owner_ids = sorted({d.agent_id for d in distributions} - {primary_agent_id})
    if unsupported_owner_ids:
        raise ValueError(
            "product portfolio projection only supports distributions on holdings owned by the "
            f"primary agent; got owner agent ids {unsupported_owner_ids}"
        )
    return Holdings(portfolio=portfolio, tlh_portfolios=tlh_portfolios, distributions=distributions)


def opening_lots(portfolio: PortfolioConfig) -> Iterator[tuple[AgentId, HoldingPositionConfig, HoldingTaxLotConfig]]:
    """Every lot with its owner and position, in holding order: the order a pool's FIFO sales walk."""

    owners = _account_owners(portfolio)
    for position in portfolio.holdings:
        for lot in position.lots:
            yield owners[position.account_id], position, lot


def _asset_id(asset: AssetKey) -> AssetId:
    """A world's flat asset identifier: a bare symbol, or the private-equity wire id."""

    return AssetId(asset.wire_id if isinstance(asset, PrivateEquityAssetKey) else asset.symbol)


def rounded_units(quantity: float, *, scale: int) -> int:
    """A float wire quantity in `scale` quanta per unit, rounded half up.

    Lot quantities cross the wire as floats, often derived as value / unit value, so they rarely
    land on a quantum; this is the one place they round.
    """

    return int((Decimal(str(quantity)) * scale).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def prepared_lots(portfolio: PortfolioConfig, *, currency: Currency) -> tuple[Lot, ...]:
    return tuple(
        Lot(
            lot_id=lot.lot_id,
            agent_id=owner,
            account_id=position.account_id,
            asset_id=_asset_id(position.asset),
            purchase_month=-lot.holding_period_months_at_start,
            quantity_scale=quantity_scale_for_asset(position.asset),
            units=rounded_units(lot.quantity, scale=quantity_scale_for_asset(position.asset)),
            basis=currency.quanta(lot.cost_basis),
        )
        for owner, position, lot in opening_lots(portfolio)
    )


def holding_pools(portfolio: PortfolioConfig) -> tuple[Pool, ...]:
    """Every pool a lot names, once."""

    pools: dict[tuple[AgentId, AccountId, AssetId], Pool] = {}
    for owner, position, _ in opening_lots(portfolio):
        pool = Pool(
            agent_id=owner,
            account_id=position.account_id,
            asset_id=_asset_id(position.asset),
            quantity_scale=quantity_scale_for_asset(position.asset),
        )
        pools[pool.agent_id, pool.account_id, pool.asset_id] = pool
    return tuple(pools.values())


def prepared_bonds(portfolio: PortfolioConfig, *, coupon_account_id: AccountId, currency: Currency) -> tuple[Bond, ...]:
    """Each bond, owned through its custody account, its coupons landing in `coupon_account_id`.

    Both months are relative to month 0: a bond bought 24 months ago and maturing in 96 is
    purchased at month -24 and matures at month 96.
    """

    owners = _account_owners(portfolio)
    return tuple(
        _prepared_bond(bond, owner=owners[bond.account_id], coupon_account_id=coupon_account_id, currency=currency)
        for bond in portfolio.bonds
    )


def prepared_tlh_portfolio(portfolio: TlhPortfolioSpec, *, currency: Currency) -> ManagedPortfolio:
    return ManagedPortfolio(
        portfolio_id=portfolio.portfolio_id,
        owner_agent_id=portfolio.owner_agent_id,
        account_id=portfolio.account_id,
        asset_id=_asset_id(portfolio.asset),
        initial_cohorts=tuple(
            TlhOpeningCohort(
                value=currency.quanta(cohort.value),
                cost_basis=currency.quanta(cohort.cost_basis),
                purchase_month_index=cohort.purchase_month_index,
            )
            for cohort in portfolio.initial_cohorts
        ),
        assumptions=portfolio.assumptions,
    )


def _account_owners(portfolio: PortfolioConfig) -> dict[AccountId, AgentId]:
    return {account.account_id: account.owner_agent_id for account in portfolio.accounts}


def _check_bond_terms(bond: BondHoldingConfig) -> None:
    """Par purchases held to maturity over whole coupon periods, the only bonds a world can value."""

    if bond.purchase_price != bond.face_value:
        raise ValueError(
            f"bond {bond.bond_id!r} was bought away from par "
            f"({bond.purchase_price=} vs {bond.face_value=}). Phase 1 supports par "
            "purchases held to maturity only: valuing a discount or premium requires the "
            "purchase yield, which is a discount factor, and phase 1 has no discount curve. "
            "Pricing bonds away from par is phase 2."
        )
    term = bond.months_to_maturity_at_start + bond.holding_period_months_at_start
    if term % bond.coupon_period_months:
        raise ValueError(
            f"bond {bond.bond_id!r} has a term of {term} months, which is not a whole number "
            f"of {bond.coupon_period_months}-month coupon periods. A stub period would need a "
            "day-count convention and an accrued-interest calculation, neither of which phase 1 has."
        )


def _prepared_bond(
    bond: BondHoldingConfig, *, owner: AgentId, coupon_account_id: AccountId, currency: Currency
) -> Bond:
    # The wire's coupon rate is a float, so it rounds onto the ppb grid.
    rate_ppb = int(round_ppb(bond.annual_coupon_rate))
    face = currency.quanta(bond.face_value)
    return Bond(
        bond_id=bond.bond_id,
        agent_id=owner,
        account_id=coupon_account_id,
        character=bond.character,
        face_value=face,
        purchase_price=currency.quanta(bond.purchase_price),
        coupon=(
            IndexedCoupon(annual_rate_ppb=rate_ppb)
            if bond.inflation_indexed
            else FixedCoupon(
                amount=coupon_amount_quanta(
                    face_quanta=face, annual_coupon_rate_ppb=rate_ppb, coupon_period_months=bond.coupon_period_months
                )
            )
        ),
        coupon_period_months=bond.coupon_period_months,
        purchase_month_index=-bond.holding_period_months_at_start,
        maturity_month_index=bond.months_to_maturity_at_start,
    )


def _distributions(
    portfolio: PortfolioConfig,
    declarations: tuple[SecurityDistributionConfig, ...],
    *,
    tlh_portfolios: tuple[TlhPortfolioSpec, ...],
    payout_account_id: AccountId,
) -> tuple[Distribution, ...]:
    """A payout for every held pool of a security the deployment declares as distributing.

    The deployment's list says WHAT a fund is made of, the portfolio says WHERE it is held. A
    pool is (owner, custody account, asset): two positions in one fund in one account are one
    pool, paid once. A declared security nobody holds contributes nothing. A TLH portfolio is a
    pool of its own: the sim pays it on the portfolio's value, not on units.
    """

    declaration_by_symbol = {declaration.symbol: declaration for declaration in declarations}
    pools: dict[tuple[AgentId, AccountId, SecuritySymbol], Distribution] = {}
    for owner, position, _ in opening_lots(portfolio):
        asset = position.asset
        if isinstance(asset, SecurityKey) and asset.symbol in declaration_by_symbol:
            pools.setdefault(
                (owner, position.account_id, asset.symbol),
                _distribution(
                    declaration_by_symbol[asset.symbol],
                    asset,
                    agent_id=owner,
                    holding_account_id=position.account_id,
                    payout_account_id=payout_account_id,
                ),
            )
    return tuple(pools.values()) + tuple(
        _distribution(
            declaration_by_symbol[managed.asset.symbol],
            managed.asset,
            agent_id=managed.owner_agent_id,
            holding_account_id=managed.account_id,
            payout_account_id=payout_account_id,
        )
        for managed in tlh_portfolios
        if managed.asset.symbol in declaration_by_symbol
    )


def _distribution(
    declaration: SecurityDistributionConfig,
    asset: SecurityKey,
    *,
    agent_id: AgentId,
    holding_account_id: AccountId,
    payout_account_id: AccountId,
) -> Distribution:
    """One pool's payout. The split must allocate the whole payout; shares of one character add, and the wire's float
    fractions round onto the ppb grid."""

    total = sum(share.fraction for share in declaration.tax_character)
    # Exactly 1, not "at most 1": a short split would silently pay out less than the fund
    # distributes, which reads as a lower yield rather than as the misconfiguration it is.
    if abs(total - 1.0) > 1e-9:
        raise ValueError(
            f"security distribution on {asset.wire_id!r} allocates {total} of its payout; "
            "the tax-character fractions must sum to 1"
        )
    fractions: dict[InterestCharacter, float] = {}
    for share in declaration.tax_character:
        fractions[share.character] = fractions.get(share.character, 0.0) + share.fraction
    return Distribution(
        agent_id=agent_id,
        holding_account_id=holding_account_id,
        asset_id=_asset_id(asset),
        to_account_id=payout_account_id,
        tax_character={
            InterestIncome(character=character): int(round_ppb(fraction)) for character, fraction in fractions.items()
        },
    )
