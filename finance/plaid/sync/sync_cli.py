"""Daily Plaid Item and transaction-cursor sync entrypoint."""

from __future__ import annotations

import asyncio
import logging
import sys

from finance.plaid.db.client import PlaidClient, PlaidCreds
from finance.plaid.db.config import PlaidSettings
from finance.plaid.db.link_store import PlaidLinkStorage
from finance.plaid.db.secret_store import K8sSecretStore
from finance.plaid.db.sync import sync_all


async def run_sync(settings: PlaidSettings) -> list[str]:
    storage = await PlaidLinkStorage.initialize(settings.database_url)
    secrets = await K8sSecretStore.from_incluster(settings.namespace, settings.managed_by)
    try:
        with PlaidClient(
            PlaidCreds(client_id=settings.client_id, secret=settings.client_secret, env=settings.plaid_env)
        ) as client:
            run_ids = await sync_all(
                api=client,
                storage=storage,
                secrets=secrets,
                trigger="cron",
                windows=settings.sync_windows,
                webhook_url=settings.webhook_url,
            )
            return [str(run_id) for run_id in run_ids]
    finally:
        await secrets.close()
        await storage.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    run_ids = asyncio.run(run_sync(PlaidSettings()))
    for run_id in run_ids:
        print(run_id)


if __name__ == "__main__":
    main()
