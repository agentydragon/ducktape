"""End-to-end Plaid sandbox smoke test.

Creates a fake public_token, exchanges it for an access_token, then pulls
accounts and one page of transactions. Verifies creds work and the airlock
PlaidProvider exchange path runs cleanly.

Run:
    set -a; source plaid/.creds.env; set +a
    bb run //plaid:sandbox_smoke
"""

import asyncio
import logging

from plaid.client import PlaidCreds, PlaidExtras, provider_for

logger = logging.getLogger(__name__)


async def async_main() -> None:
    creds = PlaidCreds.load()
    if creds.env != "sandbox":
        raise SystemExit(f"sandbox_smoke requires PLAID_ENV=sandbox, got {creds.env!r}")

    extras = PlaidExtras(creds)
    provider = provider_for(creds, products=["transactions"], redirect_uri="http://localhost/unused")

    logger.info("creating sandbox public_token …")
    public_token = await extras.sandbox_public_token_create()
    logger.info("public_token=%s…", public_token[:24])

    logger.info("exchanging for access_token via airlock PlaidProvider …")
    token = await provider.exchange_public_token(public_token)
    access_token = token.access_token
    logger.info("access_token=%s…", access_token[:24])

    logger.info("fetching /accounts/get …")
    accounts = await extras.accounts_get(access_token)
    for acct in accounts.accounts:
        bal = acct.balances
        logger.info(
            "  %-20s %-12s %-15s available=%s current=%s %s",
            acct.name,
            acct.type,
            acct.subtype,
            bal.available,
            bal.current,
            bal.iso_currency_code,
        )

    logger.info('fetching /transactions/sync (cursor="") …')
    cursor = ""
    page = 0
    while True:
        page += 1
        result = await extras.transactions_sync(access_token, cursor=cursor)
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


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
