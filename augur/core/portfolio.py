from __future__ import annotations

from datetime import date
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Literal

import yaml
from pydantic import Field, NonNegativeFloat, model_validator

from augur.core.scenario_set import (
    AccountBalance,
    AccountType,
    AssetType,
    CryptoAssetPosition,
    GenericSp500StockPosition,
    InitialBalanceSheet,
    PositionProvenance,
    PrivateEquityPosition,
)
from augur.core.schemas import ApiModel


class PortfolioAccountType(StrEnum):
    CHECKING = "checking"
    CASH_MANAGEMENT = "cash_management"
    TAXABLE_BROKERAGE = "taxable_brokerage"
    CRYPTO_EXCHANGE = "crypto_exchange"
    PRIVATE_EQUITY_PLATFORM = "private_equity_platform"


class PublicSecurityKind(StrEnum):
    GENERIC_SP500_STOCK = "generic_sp500_stock"
    ETF = "etf"
    STOCK = "stock"
    MUTUAL_FUND = "mutual_fund"
    OTHER = "other"


class ValuationMethod(StrEnum):
    STATEMENT = "statement"
    MARKET_QUOTE = "market_quote"
    MANUAL_MARK = "manual_mark"
    TENDER_OFFER = "tender_offer"
    MODEL = "model"


class PrivateEquityLiquidity(StrEnum):
    TENDER_ONLY = "tender_only"
    LOCKED = "locked"
    UNKNOWN = "unknown"


class TenderWindowStatus(StrEnum):
    SCHEDULED = "scheduled"
    OPEN = "open"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class CustodyMetadata(ApiModel):
    custodian: str = Field(description="Institution, exchange, broker, or platform holding the position.")
    source_id: str = Field(description="Stable public source id for joining downstream private composition data.")
    external_account_id: str | None = None
    external_position_id: str | None = None


class ValuationProvenance(ApiModel):
    source_id: str
    as_of: str
    method: ValuationMethod
    snapshot_id: str | None = None
    notes: str | None = None


class CostBasis(ApiModel):
    amount_usd: NonNegativeFloat
    source_id: str | None = None
    as_of: str | None = None


class PortfolioAccount(ApiModel):
    account_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    account_type: PortfolioAccountType
    owner_actor_id: str
    label: str | None = None
    cash_balance_usd: NonNegativeFloat = 0
    custody: CustodyMetadata
    valuation: ValuationProvenance


class PublicSecurityPosition(ApiModel):
    position_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    account_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    symbol: str
    security_kind: PublicSecurityKind
    market_value_usd: NonNegativeFloat
    cost_basis: CostBasis | None = None
    quantity: NonNegativeFloat | None = None
    augur_asset_type: AssetType | None = None
    custody: CustodyMetadata
    valuation: ValuationProvenance

    @model_validator(mode="after")
    def _validate_current_augur_mapping(self) -> PublicSecurityPosition:
        if self.augur_asset_type not in (None, AssetType.GENERIC_SP500_STOCK):
            raise ValueError("public security augur_asset_type must be generic_sp500_stock when set")
        return self


class CryptoHolding(ApiModel):
    position_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    account_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    asset_symbol: str
    network: str | None = None
    quantity: NonNegativeFloat
    market_value_usd: NonNegativeFloat
    cost_basis: CostBasis | None = None
    custody: CustodyMetadata
    valuation: ValuationProvenance


class PrivateEquityTenderWindow(ApiModel):
    window_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    status: TenderWindowStatus
    opens_on: date | None = None
    closes_on: date | None = None
    tenderable_units: NonNegativeFloat | None = None
    tenderable_value_usd: NonNegativeFloat | None = None
    source_id: str | None = None


class PrivateEquityLot(ApiModel):
    lot_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    account_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_\-]*$")
    issuer_id: str
    label: str | None = None
    liquidity: PrivateEquityLiquidity = PrivateEquityLiquidity.TENDER_ONLY
    mark_value_usd: NonNegativeFloat
    units: NonNegativeFloat | None = None
    cost_basis: CostBasis | None = None
    tender_windows: tuple[PrivateEquityTenderWindow, ...] = ()
    custody: CustodyMetadata
    valuation: ValuationProvenance


class PortfolioStatement(ApiModel):
    schema_version: Literal["augur.portfolio.v1"] = "augur.portfolio.v1"
    statement_id: str
    base_currency: Literal["USD"] = "USD"
    accounts: tuple[PortfolioAccount, ...]
    public_securities: tuple[PublicSecurityPosition, ...] = ()
    crypto_holdings: tuple[CryptoHolding, ...] = ()
    private_equity_lots: tuple[PrivateEquityLot, ...] = ()

    @model_validator(mode="after")
    def _validate_references(self) -> PortfolioStatement:
        account_ids = [account.account_id for account in self.accounts]
        duplicate_accounts = _duplicates(account_ids)
        if duplicate_accounts:
            raise ValueError(f"accounts must have unique account_id values: {duplicate_accounts}")

        known_accounts = set(account_ids)
        missing_account_refs = sorted(
            _unknown_public_security_account_refs(self.public_securities, known_accounts)
            | _unknown_crypto_account_refs(self.crypto_holdings, known_accounts)
            | _unknown_lot_account_refs(self.private_equity_lots, known_accounts)
        )
        if missing_account_refs:
            raise ValueError(f"positions reference unknown account_id values: {missing_account_refs}")

        position_ids = [
            *(position.position_id for position in self.public_securities),
            *(position.position_id for position in self.crypto_holdings),
            *(lot.lot_id for lot in self.private_equity_lots),
        ]
        duplicate_positions = _duplicates(position_ids)
        if duplicate_positions:
            raise ValueError(f"positions must have unique ids across all asset classes: {duplicate_positions}")

        return self

    def to_initial_balance_sheet(self) -> InitialBalanceSheet:
        account_by_id = {account.account_id: account for account in self.accounts}
        accounts = tuple(
            _scenario_account(account) for account in self.accounts if _scenario_account_type(account) is not None
        )
        # CLEANUP(2026-05-17): Tender-window data on private_equity_lots is still dropped
        # here. Generating PrivateEquitySaleOpportunityObservation rows from those windows
        # is the next slice (Half B of the funding-policies/crypto/tender slice); see
        # `augur/TODO.md`. Crypto holdings are now surfaced as first-class CryptoAssetPositions.
        assets = (
            *tuple(
                _scenario_public_security(position, account_by_id[position.account_id])
                for position in self.public_securities
                if position.augur_asset_type == AssetType.GENERIC_SP500_STOCK
            ),
            *tuple(
                _scenario_crypto_holding(holding, account_by_id[holding.account_id]) for holding in self.crypto_holdings
            ),
            *tuple(
                _scenario_private_equity_lot(lot, account_by_id[lot.account_id]) for lot in self.private_equity_lots
            ),
        )
        return InitialBalanceSheet(accounts=accounts, assets=assets)


def load_portfolio_yaml(path: str | Path) -> PortfolioStatement:
    return PortfolioStatement.model_validate(yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def load_portfolio_resource(package: str, resource_name: str) -> PortfolioStatement:
    payload = resources.files(package).joinpath(resource_name).read_text(encoding="utf-8")
    return PortfolioStatement.model_validate(yaml.safe_load(payload))


def _scenario_account(account: PortfolioAccount) -> AccountBalance:
    scenario_type = _scenario_account_type(account)
    if scenario_type is None:
        raise ValueError(f"cannot convert account type {account.account_type} to current Augur balance sheet")
    return AccountBalance(
        account_id=account.account_id,
        account_type=scenario_type,
        owner_actor_id=account.owner_actor_id,
        balance_usd=float(account.cash_balance_usd),
        provenance=_position_provenance(account.valuation, account.custody),
    )


def _scenario_account_type(account: PortfolioAccount) -> AccountType | None:
    match account.account_type:
        case PortfolioAccountType.CHECKING | PortfolioAccountType.CASH_MANAGEMENT:
            return AccountType.CHECKING
        case PortfolioAccountType.TAXABLE_BROKERAGE:
            return AccountType.TAXABLE_BROKERAGE
        case PortfolioAccountType.CRYPTO_EXCHANGE:
            return AccountType.CRYPTO_EXCHANGE
        case PortfolioAccountType.PRIVATE_EQUITY_PLATFORM:
            return None
    raise ValueError(f"unsupported portfolio account type {account.account_type}")


def _scenario_public_security(position: PublicSecurityPosition, account: PortfolioAccount) -> GenericSp500StockPosition:
    return GenericSp500StockPosition(
        asset_id=position.position_id,
        owner_actor_id=account.owner_actor_id,
        value_usd=float(position.market_value_usd),
        cost_basis_usd=float(position.cost_basis.amount_usd) if position.cost_basis is not None else None,
        provenance=_position_provenance(position.valuation, position.custody),
    )


def _scenario_crypto_holding(holding: CryptoHolding, account: PortfolioAccount) -> CryptoAssetPosition:
    return CryptoAssetPosition(
        asset_id=holding.position_id,
        owner_actor_id=account.owner_actor_id,
        value_usd=float(holding.market_value_usd),
        asset_symbol=holding.asset_symbol,
        quantity=float(holding.quantity),
        cost_basis_usd=float(holding.cost_basis.amount_usd) if holding.cost_basis is not None else None,
        source_account_id=holding.account_id,
        provenance=_position_provenance(holding.valuation, holding.custody),
    )


def _scenario_private_equity_lot(lot: PrivateEquityLot, account: PortfolioAccount) -> PrivateEquityPosition:
    return PrivateEquityPosition(
        asset_id=lot.lot_id,
        owner_actor_id=account.owner_actor_id,
        value_usd=float(lot.mark_value_usd),
        units=float(lot.units) if lot.units is not None else None,
        cost_basis_usd=float(lot.cost_basis.amount_usd) if lot.cost_basis is not None else None,
        provenance=_position_provenance(lot.valuation, lot.custody),
    )


def _position_provenance(valuation: ValuationProvenance, custody: CustodyMetadata) -> PositionProvenance:
    return PositionProvenance(
        source_id=custody.source_id, snapshot_id=valuation.snapshot_id or valuation.source_id, as_of=valuation.as_of
    )


def _duplicates(values: list[str]) -> list[str]:
    return sorted({value for value in values if values.count(value) > 1})


def _unknown_public_security_account_refs(
    positions: tuple[PublicSecurityPosition, ...], known_accounts: set[str]
) -> set[str]:
    return {position.account_id for position in positions if position.account_id not in known_accounts}


def _unknown_crypto_account_refs(positions: tuple[CryptoHolding, ...], known_accounts: set[str]) -> set[str]:
    return {position.account_id for position in positions if position.account_id not in known_accounts}


def _unknown_lot_account_refs(positions: tuple[PrivateEquityLot, ...], known_accounts: set[str]) -> set[str]:
    return {position.account_id for position in positions if position.account_id not in known_accounts}
