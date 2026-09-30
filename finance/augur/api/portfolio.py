"""User-friendly portfolio schema for Augur runtime configuration.

The deployment YAML should read like a portfolio statement: accounts contain
positions, and positions contain actual tax lots. `product/holdings.py` turns
this shape into the exact facts a world declares.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
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

from finance.augur.api.schemas import NonNegativeCurrencyAmount, PositiveCurrencyAmount
from finance.augur.model.asset_key import AssetKey, PrivateEquityAssetKey
from finance.augur.model.series import IssuerId, LevelSeriesKey, SecurityKey, SecuritySymbol
from finance.augur.sim.ids import AccountId, AgentId, BondId, LotId, PortfolioId
from finance.augur.sim.income import InterestCharacter
from finance.augur.sim.tlh import TlhAssumptions

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
    account_id: AccountId = Field(pattern=_ID_PATTERN)
    owner_agent_id: AgentId = Field(pattern=_ID_PATTERN)
    account_type: PortfolioAccountType = PortfolioAccountType.TAXABLE_BROKERAGE
    label: str | None = None


class HoldingTaxLotConfig(PortfolioConfigModel):
    lot_id: LotId = Field(pattern=_ID_PATTERN)
    holding_period_months_at_start: NonNegativeInt
    quantity: PositiveFloat
    cost_basis: NonNegativeCurrencyAmount


class HoldingAssetKind(StrEnum):
    """Discriminator for `HoldingPositionConfig` — what KIND OF THING the position is.

    Distinct from `HoldingKind`, which is display routing (etf vs stock vs mutual fund).
    This one decides how the holding is identified, and therefore which fields exist.
    """

    SECURITY = "security"
    PRIVATE_EQUITY = "private_equity"


class _HoldingPositionBase(PortfolioConfigModel):
    position_id: str = Field(pattern=_ID_PATTERN)
    account_id: AccountId = Field(pattern=_ID_PATTERN)
    label: str | None = None
    unit_value: PositiveCurrencyAmount
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
    def current_value(self) -> Decimal:
        return Decimal(str(self.total_quantity)) * self.unit_value

    @property
    def total_cost_basis(self) -> Decimal:
        return sum((lot.cost_basis for lot in self.lots), start=Decimal(0))


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
    invent a `unit_value` it cannot have (a held bond is never marked) and at least one
    `HoldingTaxLotConfig` it does not have. Sitting under `holdings` would also put it in the
    target-allocation sleeve seed, where the policy would try to sell it every month forever.

    Months are relative to month 0, like `HoldingTaxLotConfig.holding_period_months_at_start`,
    so a portfolio never mixes calendar dates with sim-relative indexes.
    """

    bond_id: BondId = Field(pattern=_ID_PATTERN)
    account_id: AccountId = Field(pattern=_ID_PATTERN)
    label: str | None = None
    character: InterestCharacter = Field(
        description=(
            "The coupon's tax character. Whether the holder owes tax on it is each of the "
            "holder's jurisdictions' rule for that character, never a property of the bond: "
            "in-state is holder-relative."
        )
    )
    face_value: PositiveCurrencyAmount
    # Carried even though the sim requires it to equal face today. Preparing the bond rejects a
    # non-par purchase explicitly so a real holding bought at 98.5 raises rather than being
    # silently treated as par — and this is where a user writes 98.5, so dropping the field
    # would defeat exactly the loud failure that check exists to produce.
    purchase_price: PositiveCurrencyAmount
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

        series_unit_values: dict[AssetKey, Decimal] = {}
        for position in self.holdings:
            unit_value = position.unit_value
            asset = position.asset
            if asset in series_unit_values and series_unit_values[asset] != unit_value:
                raise ValueError(f"portfolio positions in {asset.wire_id!r} must share unit_value")
            series_unit_values[asset] = unit_value

        return self

    @property
    def total_holdings_value(self) -> Decimal:
        return sum((position.current_value for position in self.holdings), start=Decimal(0))

    @property
    def total_bond_face_value(self) -> Decimal:
        """Face still on the books, kept apart from holdings VALUE on purpose.

        A held-to-maturity bond is never marked, so folding face into a total that means
        "what these are worth" would assert a mark the model deliberately does not produce.
        """

        return sum((bond.face_value for bond in self.bonds), start=Decimal(0))

    @property
    def level_anchors(self) -> PortfolioLevelAnchors:
        level_series_anchors: dict[LevelSeriesKey, float] = {}
        private_equity_anchors: dict[IssuerId, float] = {}
        for position in self.holdings:
            unit_value = float(position.unit_value)
            asset_key = position.asset
            if isinstance(asset_key, PrivateEquityAssetKey):
                private_equity_anchors[asset_key.issuer_id] = unit_value
            else:
                level_series_anchors[asset_key] = unit_value
        return PortfolioLevelAnchors(
            level_series_anchors=level_series_anchors, private_equity_anchors=private_equity_anchors
        )


class TlhCohort(BaseModel):
    """One tax lot of a direct-indexing statement: what it is worth, its basis and when it was bought."""

    model_config = ConfigDict(extra="forbid")

    value: NonNegativeCurrencyAmount = Field(description="Market value at the opening mark, month zero's price.")
    cost_basis: NonNegativeCurrencyAmount = Field(
        description="Remaining adjusted basis, already net of harvesting before the scenario opens."
    )
    purchase_month_index: int


class TlhPortfolioSpec(BaseModel):
    """A separately owned reduced-form portfolio, not an ordinary holding plus a policy.

    `asset` is the public index the portfolio tracks; its price series carries the cohorts' value.
    """

    model_config = ConfigDict(extra="forbid")

    portfolio_id: PortfolioId = Field(min_length=1)
    owner_agent_id: AgentId
    account_id: AccountId
    asset: SecurityKey
    initial_cohorts: list[TlhCohort]
    assumptions: TlhAssumptions


@dataclass(frozen=True)
class LabeledTlhPortfolio:
    """A managed TLH portfolio and the name the product shows it under.

    Not a `PortfolioConfig` holding: the portfolio is money-denominated and owns its cohorts,
    so it has no unit price or lots, and nothing may also hold its (owner, account, asset) pool.
    """

    spec: TlhPortfolioSpec
    label: str


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
