"""User-friendly portfolio schema for Augur runtime configuration.

This is intentionally not the simulator's executable scenario schema. The
deployment YAML should read like a portfolio statement: accounts contain
positions, and positions contain actual tax lots. The API/runtime layer expands
this shape into lower-level sim objects at the runtime boundary.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    NonNegativeFloat,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    model_validator,
)

from finance.augur.model.series import IssuerId, LevelSeriesKey, SecurityKey, SecuritySymbol
from finance.augur.product.asset_key import AssetKey, PrivateEquityAssetKey
from finance.augur.sim.scenario import BondHolding, InitialLot

_ID_PATTERN = r"^[a-z0-9][a-z0-9_\-]*$"


class PortfolioConfigModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, ignored_types=(cached_property,))


class PortfolioAccountType(StrEnum):
    TAXABLE_BROKERAGE = "taxable_brokerage"


class HoldingKind(StrEnum):
    """How to present a security holding. Display routing only — nothing downstream branches
    on it. Private equity is absent: it is a different `HoldingPositionConfig` variant, not a
    flavour of security."""

    ETF = "etf"
    STOCK = "stock"
    MUTUAL_FUND = "mutual_fund"
    # Crypto holdings (BTC, ETH, …) flow through the same position/lot machinery as stocks —
    # FIFO cost basis, cap-gains treatment, a sampled `security:*` price series. Calling them
    # "public securities" is a slight misnomer for crypto, but nothing downstream distinguishes
    # them — this enum value routes display only. The sell order names symbols, not kinds.
    CRYPTOCURRENCY = "cryptocurrency"
    OTHER = "other"


class PortfolioAccountConfig(PortfolioConfigModel):
    account_id: str = Field(pattern=_ID_PATTERN)
    owner_agent_id: str = Field(pattern=_ID_PATTERN)
    account_type: PortfolioAccountType = PortfolioAccountType.TAXABLE_BROKERAGE
    label: str | None = None


class HoldingTaxLotConfig(PortfolioConfigModel):
    lot_id: str = Field(pattern=_ID_PATTERN)
    holding_period_months_at_start: NonNegativeInt
    quantity: PositiveFloat
    cost_basis_usd: NonNegativeFloat

    @property
    def cost_basis_per_unit_usd(self) -> float:
        return float(self.cost_basis_usd / self.quantity)


class HoldingAssetKind(StrEnum):
    """Discriminator for `HoldingPositionConfig` — what KIND OF THING the position is.

    Distinct from `HoldingKind`, which is display routing (etf vs stock vs mutual fund).
    This one decides how the holding is identified, and therefore which fields exist.
    """

    SECURITY = "security"
    PRIVATE_EQUITY = "private_equity"


class _HoldingPositionBase(PortfolioConfigModel):
    position_id: str = Field(pattern=_ID_PATTERN)
    account_id: str = Field(pattern=_ID_PATTERN)
    label: str | None = None
    unit_value_usd: PositiveFloat
    lots: tuple[HoldingTaxLotConfig, ...] = Field(min_length=1)

    @property
    def asset(self) -> AssetKey:
        raise NotImplementedError

    @property
    def display_symbol(self) -> str:
        raise NotImplementedError

    @property
    def total_quantity(self) -> float:
        return sum(float(lot.quantity) for lot in self.lots)

    @property
    def current_value_usd(self) -> float:
        return self.total_quantity * float(self.unit_value_usd)

    @property
    def total_cost_basis_usd(self) -> float:
        return sum(float(lot.cost_basis_usd) for lot in self.lots)


class SecurityHoldingConfig(_HoldingPositionBase):
    """A tradable security. Its SYMBOL is its identity, all the way down.

    There is deliberately no separate "which series prices this" field. A holding whose
    price path should follow another security's says so in the MODEL, as a
    `MirrorLevelSeries` — that is a claim about markets ("VOO is the same market as SPY"),
    reviewable next to the fit, not an id buried in portfolio config. The old
    `value_series` field let the two diverge silently, which is exactly how the sell-order
    UI came to emit a symbol the compiler could not match.
    """

    kind: Literal[HoldingAssetKind.SECURITY] = HoldingAssetKind.SECURITY
    symbol: SecuritySymbol
    security_kind: HoldingKind = HoldingKind.OTHER

    @property
    def asset(self) -> AssetKey:
        return SecurityKey(symbol=self.symbol)

    @property
    def display_symbol(self) -> str:
        return str(self.symbol)


class PrivateEquityHoldingConfig(_HoldingPositionBase):
    """A private-equity holding, identified by issuer — it has no market symbol.

    `ticker` is a label some issuers have and most don't; it never identifies anything.
    """

    kind: Literal[HoldingAssetKind.PRIVATE_EQUITY] = HoldingAssetKind.PRIVATE_EQUITY
    issuer_id: IssuerId
    ticker: str | None = None

    @property
    def asset(self) -> AssetKey:
        return PrivateEquityAssetKey(issuer_id=self.issuer_id)

    @property
    def display_symbol(self) -> str:
        return self.ticker or str(self.issuer_id)


type HoldingPositionConfig = Annotated[SecurityHoldingConfig | PrivateEquityHoldingConfig, Field(discriminator="kind")]


class BondHoldingConfig(PortfolioConfigModel):
    """A bond held at scenario start, in the deployment's idiom.

    Deliberately NOT a third `HoldingPositionConfig` variant. A bond is not a tax lot — the
    sim says so structurally, giving it its own table and excluding it from liquid net worth
    rather than relying on a rule someone remembers — and the position base would force it to
    invent a `unit_value_usd` it cannot have (a held bond is never marked) and at least one
    `HoldingTaxLotConfig` it does not have. Sitting under `holdings` would also put it in the
    target-allocation sleeve seed, where the policy would try to sell it every month forever.

    Months are relative to month 0, like `HoldingTaxLotConfig.holding_period_months_at_start`,
    so a portfolio never mixes calendar dates with sim-relative indexes.
    """

    bond_id: str = Field(pattern=_ID_PATTERN)
    account_id: str = Field(pattern=_ID_PATTERN)
    label: str | None = None
    issuer_jurisdiction_id: str | None = Field(
        default=None,
        description=(
            "The taxing authority that issued the debt — `federal_us` for a Treasury, "
            "`california` for a CA muni, `None` for a corporate issuer. Whether the holder "
            "owes tax on the coupon is a relation between this issuer and the holder's own "
            "jurisdictions, never a property of the bond: in-state is holder-relative."
        ),
    )
    face_value_usd: PositiveFloat
    # Carried even though the sim requires it to equal face today. The sim rejects a non-par
    # purchase explicitly so a real holding bought at 98.5 raises rather than being silently
    # treated as par — and this is where a user writes 98.5, so dropping the field would
    # defeat exactly the loud failure that validator exists to produce.
    purchase_price_usd: PositiveFloat
    annual_coupon_rate: NonNegativeFloat
    coupon_period_months: PositiveInt = 6
    inflation_indexed: bool = False
    holding_period_months_at_start: NonNegativeInt = 0
    months_to_maturity_at_start: PositiveInt


class PortfolioConfig(PortfolioConfigModel):
    """Deployment-authored portfolio facts.

    Month 0 is the start of the simulated scenario. Tax lots express their
    holding period relative to month 0, avoiding a mix of calendar dates and
    sim-relative month indexes.
    """

    accounts: tuple[PortfolioAccountConfig, ...] = ()
    holdings: tuple[HoldingPositionConfig, ...] = ()
    # Separate from `holdings` for the reasons on `BondHoldingConfig`: a bond is not a tax lot,
    # is never marked, and must not reach the target-allocation sleeve seed.
    bonds: tuple[BondHoldingConfig, ...] = ()

    @model_validator(mode="after")
    def _validate_references(self) -> PortfolioConfig:
        duplicate_accounts = _duplicates(account.account_id for account in self.accounts)
        if duplicate_accounts:
            raise ValueError(f"portfolio accounts must have unique account_id values: {duplicate_accounts}")

        known_accounts = {account.account_id for account in self.accounts}
        missing_accounts = sorted(
            {position.account_id for position in self.holdings if position.account_id not in known_accounts}
        )
        if missing_accounts:
            raise ValueError(f"portfolio positions reference unknown account_id values: {missing_accounts}")

        # Checked here rather than left to the sim's account resolution: a bond pointing at an
        # account that does not exist has nowhere to pay its coupons.
        missing_bond_accounts = sorted(
            {bond.account_id for bond in self.bonds if bond.account_id not in known_accounts}
        )
        if missing_bond_accounts:
            raise ValueError(f"portfolio bonds reference unknown account_id values: {missing_bond_accounts}")

        duplicate_bonds = _duplicates(bond.bond_id for bond in self.bonds)
        if duplicate_bonds:
            raise ValueError(f"portfolio bonds must have unique bond_id values: {duplicate_bonds}")

        duplicate_positions = _duplicates(position.position_id for position in self.holdings)
        if duplicate_positions:
            raise ValueError(f"public securities must have unique position_id values: {duplicate_positions}")

        duplicate_lots = _duplicates(lot.lot_id for position in self.holdings for lot in position.lots)
        if duplicate_lots:
            raise ValueError(f"public security tax lots must have unique lot_id values: {duplicate_lots}")

        series_unit_values: dict[AssetKey, float] = {}
        for position in self.holdings:
            unit_value = float(position.unit_value_usd)
            asset = position.asset
            if asset in series_unit_values and series_unit_values[asset] != unit_value:
                raise ValueError(f"portfolio positions in {asset.wire_id!r} must share unit_value_usd")
            series_unit_values[asset] = unit_value

        return self

    @property
    def total_holdings_value_usd(self) -> float:
        return sum(position.current_value_usd for position in self.holdings)

    @property
    def total_bond_face_value_usd(self) -> float:
        """Face still on the books, kept apart from holdings VALUE on purpose.

        A held-to-maturity bond is never marked, so folding face into a total that means
        "what these are worth" would assert a mark the model deliberately does not produce.
        """

        return sum(float(bond.face_value_usd) for bond in self.bonds)

    @property
    def level_anchors(self) -> PortfolioLevelAnchors:
        level_series_anchors: dict[LevelSeriesKey, float] = {}
        private_equity_anchors: dict[IssuerId, float] = {}
        for position in self.holdings:
            unit_value = float(position.unit_value_usd)
            asset_key = position.asset
            if isinstance(asset_key, PrivateEquityAssetKey):
                private_equity_anchors[asset_key.issuer_id] = unit_value
            else:
                level_series_anchors[asset_key] = unit_value
        return PortfolioLevelAnchors(
            level_series_anchors=level_series_anchors, private_equity_anchors=private_equity_anchors
        )

    def to_initial_lots(self) -> tuple[InitialLot, ...]:
        account_by_id = {account.account_id: account for account in self.accounts}
        return tuple(
            InitialLot(
                lot_id=lot.lot_id,
                agent_id=account_by_id[position.account_id].owner_agent_id,
                account_id=position.account_id,
                asset=position.asset,
                purchase_month_index=-int(lot.holding_period_months_at_start),
                quantity=float(lot.quantity),
                cost_basis_per_unit_usd=lot.cost_basis_per_unit_usd,
            )
            for position in self.holdings
            for lot in position.lots
        )

    def to_initial_bonds(self, *, coupon_account_id: str) -> tuple[BondHolding, ...]:
        """The bond mirror of `to_initial_lots`, with the same two conversions plus a routing.

        The owner comes through the CUSTODY account, not the bond — the account is the
        owner-bearing object. Both months are relative to month 0: a bond bought 24 months ago
        and maturing in 96 lands at `purchase_month_index=-24`, `maturity_month_index=96`.

        `coupon_account_id` is required and has no default because a bond's config account is
        where it is HELD, and the sim's is where its coupons LAND — two different things that
        are the same string only by coincidence. Portfolio accounts are custody accounts
        (`taxable_brokerage`); they carry no cash row, so a coupon paid into one would have
        nowhere to go. The caller knows its own cash topology, so it names the destination.
        This mirrors sale proceeds, which already land in the funding account rather than in
        the account whose lots were sold.
        """

        account_by_id = {account.account_id: account for account in self.accounts}
        return tuple(
            BondHolding(
                bond_id=bond.bond_id,
                agent_id=account_by_id[bond.account_id].owner_agent_id,
                account_id=coupon_account_id,
                issuer_jurisdiction_id=bond.issuer_jurisdiction_id,
                face_value_usd=bond.face_value_usd,
                purchase_price_usd=bond.purchase_price_usd,
                annual_coupon_rate=bond.annual_coupon_rate,
                coupon_period_months=bond.coupon_period_months,
                inflation_indexed=bond.inflation_indexed,
                purchase_month_index=-int(bond.holding_period_months_at_start),
                maturity_month_index=int(bond.months_to_maturity_at_start),
            )
            for bond in self.bonds
        )


@dataclass(frozen=True)
class PortfolioLevelAnchors:
    """Typed split of portfolio month-0 anchors.

    Non-PE level series anchors flow into the exogenous bundle's `levels` frame
    via `LevelSeriesKey`; PE issuer anchors flow into the `PrivateEquityBundle`
    keyed by `IssuerId`. The split lives at the API/runtime boundary, dispatching
    on each holding's typed `asset` `AssetKey`.
    """

    level_series_anchors: dict[LevelSeriesKey, float]
    private_equity_anchors: dict[IssuerId, float]


def _duplicates(values) -> list[str]:
    counts = Counter(values)
    return sorted(value for value, count in counts.items() if count > 1)
