"""End-to-end Plaid sandbox smoke test.

Creates a fake public_token, exchanges it for an access_token, then pulls accounts
and a page of transactions straight off the SDK client. Verifies the creds and the
SDK call path run cleanly.

Run:
    set -a; source finance/plaid/db/.creds.env; set +a
    bb run //finance/plaid/db:sandbox_smoke
"""

import logging
from typing import TypedDict, cast

from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.products import Products
from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest

from finance.plaid.db.client import PlaidClient
from finance.plaid.db.dev_creds import load

logger = logging.getLogger(__name__)


class _BalancePayload(TypedDict, total=False):
    available: object
    current: object
    iso_currency_code: object


class _AccountPayload(TypedDict, total=False):
    name: object
    type: object
    subtype: object
    balances: _BalancePayload


class _AccountsPayload(TypedDict):
    accounts: list[_AccountPayload]


class _TransactionPayload(TypedDict, total=False):
    date: object
    amount: object
    iso_currency_code: object
    name: object
    merchant_name: object


class _TransactionsSyncPayload(TypedDict, total=False):
    added: list[_TransactionPayload]
    has_more: bool
    next_cursor: str


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    creds = load()
    if creds.env != "sandbox":
        raise SystemExit(f"sandbox_smoke requires PLAID_ENV=sandbox, got {creds.env!r}")

    with PlaidClient(creds) as api:
        logger.info("creating sandbox public_token …")
        public_token = api.sandbox_public_token_create(
            SandboxPublicTokenCreateRequest(institution_id="ins_109508", initial_products=[Products("transactions")])
        ).public_token
        logger.info("public_token=%s…", public_token[:24])

        logger.info("exchanging for access_token …")
        access_token = api.item_public_token_exchange(
            ItemPublicTokenExchangeRequest(public_token=public_token)
        ).access_token
        logger.info("access_token=%s…", access_token[:24])

        logger.info("fetching /accounts/get …")
        accounts_payload = cast(
            _AccountsPayload, api.accounts_get(AccountsGetRequest(access_token=access_token)).to_dict()
        )
        for acct in accounts_payload["accounts"]:
            bal = acct.get("balances", {})
            logger.info(
                "  %-20s %-12s %-15s available=%s current=%s %s",
                acct.get("name"),
                acct.get("type"),
                acct.get("subtype"),
                bal.get("available"),
                bal.get("current"),
                bal.get("iso_currency_code"),
            )

        logger.info('fetching /transactions/sync (cursor="") …')
        cursor = ""
        page = 0
        while True:
            page += 1
            request = TransactionsSyncRequest(access_token=access_token, count=500)
            if cursor:
                request.cursor = cursor
            result = cast(_TransactionsSyncPayload, api.transactions_sync(request).to_dict())
            added = result.get("added", [])
            logger.info("  page=%d added=%d has_more=%s", page, len(added), result.get("has_more"))
            for txn in added[:5]:
                logger.info(
                    "    %s  %8.2f %s  %s",
                    txn.get("date"),
                    txn.get("amount", 0),
                    txn.get("iso_currency_code") or "",
                    txn.get("name") or txn.get("merchant_name") or "",
                )
            if not result.get("has_more"):
                break
            cursor = result["next_cursor"]
            if page >= 10:
                logger.info("  stopping after 10 pages")
                break


if __name__ == "__main__":
    main()
