"""Plaid MCP server: read-only transactions, balances, and credit-card liabilities.

Auth-oblivious by design — a front proxy (`mcp-oauth-facade`) handles Authentik
OAuth; this server only speaks MCP over HTTP on its configured port. One server
holds every configured item's access token; tools take an `item` selector.
"""

import logging
import sys
from datetime import date
from typing import Annotated

import uvicorn
from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from plaid.client import PlaidCreds, PlaidExtras
from plaid.mcp_server.config import ResolvedItem, ServerSettings
from plaid.mcp_server.projections import (
    AccountOut,
    CardLiabilityOut,
    ItemSummary,
    TransactionPage,
    account_out,
    card_liability_out,
    transaction_out,
)

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Read-only access to the owner's Plaid-linked bank accounts: transactions, balances, and "
    "credit-card liabilities. Call list_items first to discover the `item` selectors and which "
    "products each supports. Transaction amount sign: positive = money out (charges/debits), "
    "negative = money in (payments/refunds/deposits)."
)

_ItemArg = Annotated[str, Field(description="Item selector from list_items, e.g. 'chase' or 'bofa'.")]


def build_server(extras: PlaidExtras, items: dict[str, ResolvedItem]) -> FastMCP:
    mcp: FastMCP = FastMCP("Plaid MCP", instructions=INSTRUCTIONS)

    def resolve(item: str) -> ResolvedItem:
        resolved = items.get(item)
        if resolved is None:
            raise ToolError(f"Unknown item {item!r}. Valid items: {sorted(items)}.")
        return resolved

    @mcp.tool
    def list_items() -> list[ItemSummary]:
        """List the configured Plaid items.

        Call this first: each `key` is a valid `item` argument for the other tools, and only
        items whose `products` include 'liabilities' accept get_credit_card_liabilities.
        """
        return [ItemSummary(key=i.key, institution=i.institution, products=i.products) for i in items.values()]

    @mcp.tool
    async def list_accounts(item: _ItemArg) -> list[AccountOut]:
        """Accounts for an item with CACHED balances.

        Balances reflect Plaid's last pull (refreshed 1-4x/day); use get_live_balance for a
        real-time figure. The returned account_id values feed the filters on the other tools.
        """
        resp = await extras.accounts_get(resolve(item).access_token)
        return [account_out(a) for a in resp.accounts]

    @mcp.tool
    async def list_transactions(
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
        account_ids = [account_id] if account_id is not None else None
        resp = await extras.transactions_get(
            resolve(item).access_token,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            account_ids=account_ids,
            offset=offset,
            count=count,
        )
        return TransactionPage(
            total=resp.total_transactions, transactions=[transaction_out(t) for t in resp.transactions]
        )

    @mcp.tool
    async def get_credit_card_liabilities(item: _ItemArg) -> list[CardLiabilityOut]:
        """Credit-card liabilities: statement balance, due dates, minimum payment, and APRs.

        Backed by /liabilities/get; credit-card accounts only. Valid only for items whose
        products include 'liabilities' (see list_items). Most fields are nullable and
        issuer-dependent, and `aprs` is often empty (many issuers don't report APRs).
        """
        resolved = resolve(item)
        if "liabilities" not in resolved.products:
            raise ToolError(
                f"Item {resolved.key!r} has no 'liabilities' product (products: {resolved.products}). "
                "Use list_items to see which items support liabilities."
            )
        resp = await extras.liabilities_get(resolved.access_token)
        accounts_by_id = {a.account_id: a for a in resp.accounts}
        credit = resp.liabilities.credit or []
        return [card_liability_out(c, accounts_by_id) for c in credit]

    @mcp.tool
    async def get_live_balance(
        item: _ItemArg,
        account_id: Annotated[
            str | None,
            Field(description="Restrict to one account_id to conserve the per-item rate budget; omit for all."),
        ] = None,
    ) -> list[AccountOut]:
        """Real-time balances via /accounts/balance/get (hits the bank, uncached).

        Heavily rate-limited: 5/min and 30/hour per item. Prefer list_accounts (cached) for
        routine reads; pass account_id to fetch a single account and conserve the budget.
        """
        account_ids = [account_id] if account_id is not None else None
        resp = await extras.accounts_balance_get(resolve(item).access_token, account_ids=account_ids)
        return [account_out(a) for a in resp.accounts]

    return mcp


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    settings = ServerSettings()
    items = settings.resolved_items()
    creds = PlaidCreds(client_id=settings.client_id, secret=settings.client_secret, env=settings.plaid_env)
    mcp = build_server(PlaidExtras(creds), items)
    logger.info("plaid-mcp listening on %s:%d (items: %s)", settings.host, settings.port, sorted(items))
    uvicorn.run(mcp.http_app(path="/mcp"), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
