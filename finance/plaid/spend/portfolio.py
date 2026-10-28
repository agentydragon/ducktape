"""Read a current USD portfolio from the Plaid mirror and optional Coinbase."""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation

import httpx
import jwt
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from finance.plaid.spend.plaid_valuation import (
    AccountObservation,
    HoldingObservation,
    PlaidValuation,
    PlaidValuationError,
    decimal_amount,
    unavailable_source,
    usd_minor_units,
    value_plaid_rows,
)
from finance.plaid.spend.portfolio_models import PortfolioPolicy, PortfolioView, SourceValuation
from finance.plaid.spend.portfolio_queries import select_account_balances, select_holding_batches

_COINBASE_HOST = "api.coinbase.com"
_COINBASE_ACCOUNTS_PATH = "/api/v3/brokerage/accounts"
_COINBASE_ACCOUNTS_URL = f"https://{_COINBASE_HOST}{_COINBASE_ACCOUNTS_PATH}"
_COINBASE_PRICE_URL = "https://api.coinbase.com/v2/prices/{asset}-USD/spot"
_ASSET_CODE = re.compile(r"^[A-Z0-9]{2,20}$")


class _CoinbaseMoney(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    value: str
    currency: str


class _CoinbaseAccount(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    uuid: str
    currency: str
    available_balance: _CoinbaseMoney
    hold: _CoinbaseMoney


class _CoinbaseAccountsPage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    accounts: list[_CoinbaseAccount]
    has_next: bool
    cursor: str = ""


class _CoinbaseSpotPriceData(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    base: str
    currency: str
    amount: str


class _CoinbaseSpotPrice(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    data: _CoinbaseSpotPriceData


async def _read_plaid(
    session_factory: async_sessionmaker[AsyncSession], policy: PortfolioPolicy, *, now: datetime
) -> PlaidValuation:
    plaid = policy.sources.plaid
    selected_ids = [*plaid.asset_account_ids, *plaid.liability_account_ids]
    required_holding_accounts = {exclusion.account_id for exclusion in policy.excluded_securities} | {
        correction.account_id for correction in plaid.holding_value_corrections
    }
    if not selected_ids:
        raise PlaidValuationError("No Plaid accounts are selected.")

    try:
        async with session_factory() as session:
            account_result = await session.execute(select_account_balances(selected_ids))
            account_rows = account_result.mappings().all()
            holding_rows: Sequence[RowMapping] = ()
            if required_holding_accounts:
                account_ids = sorted(required_holding_accounts)
                holding_result = await session.execute(select_holding_batches(account_ids))
                holding_rows = holding_result.mappings().all()
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        raise PlaidValuationError("The Plaid mirror could not be read.") from exc

    try:
        accounts = [AccountObservation.model_validate(dict(row)) for row in account_rows]
        holdings = [HoldingObservation.model_validate(dict(row)) for row in holding_rows]
    except ValidationError as exc:
        raise PlaidValuationError("The Plaid mirror returned malformed valuation observations.") from exc
    return value_plaid_rows(policy, account_rows=accounts, holding_rows=holdings, now=now)


def _coinbase_jwt(api_key: SecretStr, api_secret: SecretStr, *, now: datetime, method: str, path: str) -> str:
    issued_at = int(now.timestamp())
    uri = f"{method} {_COINBASE_HOST}{path}"
    token = jwt.encode(
        {"iss": "cdp", "sub": api_key.get_secret_value(), "nbf": issued_at, "exp": issued_at + 120, "uri": uri},
        api_secret.get_secret_value(),
        algorithm="ES256",
        headers={"kid": api_key.get_secret_value(), "nonce": str(uuid.uuid4())},
    )
    return token.decode("ascii") if isinstance(token, bytes) else token


class CoinbaseClient:
    """Authenticated read-only Coinbase API client."""

    def __init__(self, *, api_key: SecretStr, api_secret: SecretStr) -> None:
        self._api_key = api_key
        self._api_secret = api_secret

    async def get_accounts_page(
        self, client: httpx.AsyncClient, *, now: datetime, cursor: str
    ) -> _CoinbaseAccountsPage:
        params = {"limit": "250"}
        if cursor:
            params["cursor"] = cursor
        token = _coinbase_jwt(self._api_key, self._api_secret, now=now, method="GET", path=_COINBASE_ACCOUNTS_PATH)
        response = await client.get(_COINBASE_ACCOUNTS_URL, params=params, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        return _CoinbaseAccountsPage.model_validate(response.json())

    async def get_spot_price(self, client: httpx.AsyncClient, *, asset: str) -> _CoinbaseSpotPriceData:
        response = await client.get(_COINBASE_PRICE_URL.format(asset=asset))
        response.raise_for_status()
        return _CoinbaseSpotPrice.model_validate(response.json()).data


async def _read_coinbase(*, now: datetime, coinbase_client: CoinbaseClient | None) -> tuple[SourceValuation, int]:
    if coinbase_client is None:
        return unavailable_source("coinbase", "Coinbase is enabled but its view-only credentials are unavailable."), 0
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            accounts: list[_CoinbaseAccount] = []
            cursors: set[str] = set()
            cursor = ""
            while True:
                page = await coinbase_client.get_accounts_page(client, now=now, cursor=cursor)
                accounts.extend(page.accounts)
                if not page.has_next:
                    break
                if not page.cursor or page.cursor in cursors:
                    return unavailable_source("coinbase", "Coinbase account pagination was incomplete."), 0
                cursors.add(page.cursor)
                cursor = page.cursor

            account_ids = [account.uuid for account in accounts]
            if len(account_ids) != len(set(account_ids)):
                return unavailable_source("coinbase", "Coinbase returned an ambiguous account set."), 0
            currencies: set[str] = set()
            account_values: list[tuple[str, Decimal]] = []
            for account in accounts:
                currency = account.currency
                if not _ASSET_CODE.fullmatch(currency):
                    return unavailable_source("coinbase", "Coinbase returned an unsupported asset code."), 0
                if account.available_balance.currency != currency or account.hold.currency != currency:
                    return unavailable_source("coinbase", "Coinbase balance currencies did not match the account."), 0
                available = decimal_amount(account.available_balance.value)
                hold = decimal_amount(account.hold.value)
                if available is None or hold is None:
                    return unavailable_source("coinbase", "Coinbase returned a malformed balance."), 0
                value = available + hold
                account_values.append((currency, value))
                if currency != "USD" and value != 0:
                    currencies.add(currency)

            prices: dict[str, Decimal] = {"USD": Decimal(1)}
            for currency in sorted(currencies):
                price = await coinbase_client.get_spot_price(client, asset=currency)
                amount = decimal_amount(price.amount)
                if price.base != currency or price.currency != "USD" or amount is None or amount <= 0:
                    return unavailable_source("coinbase", "Coinbase returned a malformed USD spot quote."), 0
                prices[currency] = amount
    except httpx.HTTPError, InvalidOperation, TypeError, ValueError, ValidationError, jwt.PyJWTError:
        return unavailable_source("coinbase", "Coinbase could not provide a complete current valuation."), 0

    total_usd = sum((value * prices[currency] for currency, value in account_values if value != 0), Decimal(0))
    return (
        SourceValuation(
            name="coinbase",
            status="ready",
            amount_minor_units=usd_minor_units(total_usd),
            as_of=None,
            issues=[
                "Coinbase values use fetched balances and request-time spot prices where needed; "
                "provider quote timestamps are unavailable."
            ],
        ),
        usd_minor_units(total_usd),
    )


async def read_portfolio(
    session_factory: async_sessionmaker[AsyncSession],
    policy: PortfolioPolicy,
    *,
    now: datetime,
    coinbase_client: CoinbaseClient | None = None,
) -> PortfolioView:
    """Read selected holdings and return a fail-closed USD portfolio view."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    sources: list[SourceValuation] = []
    assets_minor_units = 0
    liabilities_minor_units = 0
    excluded_minor_units = 0
    sources_ready = True
    selected = bool(policy.sources.plaid.asset_account_ids or policy.sources.plaid.liability_account_ids)
    if selected:
        try:
            valuation = await _read_plaid(session_factory, policy, now=now)
        except PlaidValuationError as exc:
            sources.append(unavailable_source("plaid", str(exc)))
            sources_ready = False
        else:
            sources.append(valuation.source())
            assets_minor_units += valuation.assets_minor_units
            liabilities_minor_units += valuation.liabilities_minor_units
            excluded_minor_units += valuation.excluded_minor_units

    if policy.sources.coinbase.enabled:
        coinbase_source, coinbase_assets = await _read_coinbase(now=now, coinbase_client=coinbase_client)
        sources.append(coinbase_source)
        if coinbase_source.status == "unavailable":
            sources_ready = False
        if selected and coinbase_source.status == "ready":
            coinbase_source = coinbase_source.model_copy(
                update={
                    "issues": [
                        *coinbase_source.issues,
                        "Coinbase balances were fetched during this valuation while Plaid balances are cached "
                        "institution observations.",
                    ]
                }
            )
            sources[-1] = coinbase_source
        if coinbase_source.status == "ready":
            assets_minor_units += coinbase_assets

    if not sources or not sources_ready:
        return PortfolioView(
            status="unavailable",
            net_minor_units=None,
            assets_minor_units=None,
            liabilities_minor_units=None,
            excluded_minor_units=None,
            sources=sources,
        )
    return PortfolioView(
        status="ready",
        net_minor_units=assets_minor_units - liabilities_minor_units - excluded_minor_units,
        assets_minor_units=assets_minor_units,
        liabilities_minor_units=liabilities_minor_units,
        excluded_minor_units=excluded_minor_units,
        sources=sources,
    )
