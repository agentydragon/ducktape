"""Plaid MCP server: read-only transactions, balances, and liabilities.

Auth-oblivious by design — a front proxy (`mcp-oauth-facade`) handles Authentik OAuth;
this server only speaks MCP over HTTP on its configured port. One server holds every
configured item's access token; tools take an `item` selector.

Tools call the plaid-python SDK client directly, run its responses through
`sanitize_for_serialization`, and validate them into the typed `plaid_utils.models` shapes
(the models already cover only the fields we expose). The SDK's `ApiException` propagates
to FastMCP's error boundary, which surfaces Plaid's message (PRODUCT_NOT_READY,
ITEM_LOGIN_REQUIRED, RATE_LIMIT_EXCEEDED, ...) to the agent.
"""

import logging
import sys
from datetime import date
from typing import Annotated, Any, Protocol

import uvicorn
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest
from plaid.model.accounts_balance_get_request_options import AccountsBalanceGetRequestOptions
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions
from pydantic import BaseModel, Field

from plaid_utils.client import PlaidCreds, plaid_client
from plaid_utils.mcp_server.config import ResolvedItem, ServerSettings
from plaid_utils.models import (
    Account,
    AccountsGetResponse,
    Liabilities,
    LiabilitiesGetResponse,
    TransactionPage,
    TransactionsGetResponse,
)

logger = logging.getLogger(__name__)


class ItemSummary(BaseModel):
    """list_items output: a configured item without its access token."""

    key: str
    institution: str
    products: list[str]


class PlaidApiLike(Protocol):
    """The slice of `plaid_api.PlaidApi` the server uses, so tests can inject a fake.

    Requests/responses are the SDK's own types (`Any` here); `api_client` carries
    `sanitize_for_serialization`. The real `plaid_api.PlaidApi` satisfies it structurally.
    """

    api_client: Any

    def accounts_get(self, request: Any, /) -> Any: ...
    def accounts_balance_get(self, request: Any, /) -> Any: ...
    def transactions_get(self, request: Any, /) -> Any: ...
    def liabilities_get(self, request: Any, /) -> Any: ...


INSTRUCTIONS = (
    "Read-only access to the owner's Plaid-linked bank accounts: transactions, balances, and "
    "liabilities (credit cards, mortgages, student loans). Call list_items first to discover the "
    "`item` selectors and which products each supports. Transaction amount sign: positive = money "
    "out (charges/debits), negative = money in (payments/refunds/deposits)."
)

_ItemArg = Annotated[str, Field(description="Item selector from list_items, e.g. 'chase' or 'bofa'.")]


def build_server(api: PlaidApiLike, items: dict[str, ResolvedItem]) -> FastMCP:
    mcp: FastMCP = FastMCP("Plaid MCP", instructions=INSTRUCTIONS)
    sanitize = api.api_client.sanitize_for_serialization

    def resolve(item: str) -> ResolvedItem:
        resolved = items.get(item)
        if resolved is None:
            raise ToolError(f"Unknown item {item!r}. Valid items: {sorted(items)}.")
        return resolved

    @mcp.tool
    def list_items() -> list[ItemSummary]:
        """List the configured Plaid items.

        Call this first: each `key` is a valid `item` argument for the other tools, and only
        items whose `products` include 'liabilities' accept get_liabilities.
        """
        return [ItemSummary(key=i.key, institution=i.institution, products=i.products) for i in items.values()]

    @mcp.tool
    def list_accounts(item: _ItemArg) -> list[Account]:
        """Accounts for an item with CACHED balances.

        Balances reflect Plaid's last pull (refreshed 1-4x/day); use get_live_balance for a
        real-time figure. The returned account_id values feed the filters on the other tools.
        """
        resp = api.accounts_get(AccountsGetRequest(access_token=resolve(item).access_token))
        return AccountsGetResponse.model_validate(sanitize(resp)).accounts

    @mcp.tool
    def list_transactions(
        item: _ItemArg,
        start_date: Annotated[date, Field(description="Inclusive start date.")],
        end_date: Annotated[date, Field(description="Inclusive end date.")],
        account_id: Annotated[
            str | None, Field(description="Restrict to one account_id (from list_accounts); omit for all accounts.")
        ] = None,
        offset: Annotated[int, Field(description="Pagination offset within the date range.", ge=0)] = 0,
        count: Annotated[int, Field(description="Page size.", ge=1, le=500)] = 50,
    ) -> TransactionPage:
        """Transactions in [start_date, end_date] (inclusive), paged with offset/count.

        Backed by /transactions/get. `total` is the full count in the range before slicing, so
        page until offset+count >= total. Amount sign: positive = money out (charges/debits),
        negative = money in (payments/refunds/deposits). A pending=true row is later replaced by
        a posted row whose pending_transaction_id points back to the pending id (dedupe on it).
        Recently linked/refreshed items can briefly raise PRODUCT_NOT_READY.
        """
        options = TransactionsGetRequestOptions(offset=offset, count=count)
        if account_id is not None:
            options.account_ids = [account_id]
        resp = api.transactions_get(
            TransactionsGetRequest(
                access_token=resolve(item).access_token, start_date=start_date, end_date=end_date, options=options
            )
        )
        parsed = TransactionsGetResponse.model_validate(sanitize(resp))
        return TransactionPage(total=parsed.total_transactions, transactions=parsed.transactions)

    @mcp.tool
    def get_liabilities(item: _ItemArg) -> Liabilities:
        """Liabilities for an item: `credit` cards, `mortgage`s, and `student` loans.

        Backed by /liabilities/get. Each array is null when the item has no accounts of that
        type (e.g. a card-only item returns mortgage=null, student=null). Valid only for items
        whose products include 'liabilities' (see list_items). Most fields are nullable and
        issuer-dependent — e.g. a credit card's `aprs` is often empty. Each entry carries an
        account_id; correlate with list_accounts for the account name/mask.
        """
        resolved = resolve(item)
        if "liabilities" not in resolved.products:
            raise ToolError(
                f"Item {resolved.key!r} has no 'liabilities' product (products: {resolved.products}). "
                "Use list_items to see which items support liabilities."
            )
        resp = api.liabilities_get(LiabilitiesGetRequest(access_token=resolved.access_token))
        return LiabilitiesGetResponse.model_validate(sanitize(resp)).liabilities

    @mcp.tool
    def get_live_balance(
        item: _ItemArg,
        account_id: Annotated[
            str | None,
            Field(description="Restrict to one account_id to conserve the per-item rate budget; omit for all."),
        ] = None,
    ) -> list[Account]:
        """Real-time balances via /accounts/balance/get (hits the bank, uncached).

        Heavily rate-limited: 5/min and 30/hour per item. Prefer list_accounts (cached) for
        routine reads; pass account_id to fetch a single account and conserve the budget.
        """
        request = AccountsBalanceGetRequest(access_token=resolve(item).access_token)
        if account_id is not None:
            request.options = AccountsBalanceGetRequestOptions(account_ids=[account_id])
        resp = api.accounts_balance_get(request)
        return AccountsGetResponse.model_validate(sanitize(resp)).accounts

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    settings = ServerSettings()
    items = settings.resolved_items()
    api = plaid_client(PlaidCreds(client_id=settings.client_id, secret=settings.client_secret, env=settings.plaid_env))
    mcp = build_server(api, items)
    logger.info("plaid-mcp listening on %s:%d (items: %s)", settings.host, settings.port, sorted(items))
    uvicorn.run(mcp.http_app(path="/mcp"), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
