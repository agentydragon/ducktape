"""End-to-end Plaid sandbox smoke test.

Creates a fake public_token, exchanges it for an access_token, then pulls accounts
and a page of transactions straight off the SDK client. Verifies the creds and the
SDK call path run cleanly.

Run:
    set -a; source finance/plaid/db/.creds.env; set +a
    bb run //finance/plaid/db:sandbox_smoke_bin
"""

import logging

from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.products import Products
from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest

from finance.plaid.db.client import PlaidClient
from finance.plaid.db.dev_creds import load
from finance.plaid.db.models import AccountsGetResponse, TransactionsSyncResponse

logger = logging.getLogger(__name__)


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
        accounts_payload = AccountsGetResponse.model_validate(
            api.accounts_get(AccountsGetRequest(access_token=access_token)).to_dict()
        )
        for acct in accounts_payload.accounts or []:
            bal = acct.balances
            logger.info(
                "  %-20s %-12s %-15s available=%s current=%s %s",
                acct.name,
                acct.type,
                acct.subtype,
                bal.available if bal else None,
                bal.current if bal else None,
                bal.iso_currency_code if bal else None,
            )

        logger.info('fetching /transactions/sync (cursor="") …')
        cursor = ""
        page = 0
        while True:
            page += 1
            request = TransactionsSyncRequest(access_token=access_token, count=500)
            if cursor:
                request.cursor = cursor
            result = TransactionsSyncResponse.model_validate(api.transactions_sync(request).to_dict())
            added = result.added
            logger.info("  page=%d added=%d has_more=%s", page, len(added), result.has_more)
            for txn in added[:5]:
                logger.info(
                    "    %s  %8.2f %s  %s",
                    txn.date,
                    txn.amount,
                    txn.iso_currency_code or "",
                    txn.name or txn.merchant_name or "",
                )
            if not result.has_more:
                break
            cursor = result.next_cursor
            if page >= 10:
                logger.info("  stopping after 10 pages")
                break


if __name__ == "__main__":
    main()
