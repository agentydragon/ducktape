"""Sets the gateway's remote syslog (`syslog.ha`, which sends its firewall log) to the
configured server, port and level if it differs, and reads it back. One pass per run; it
raises when the setting does not hold, so a scheduled run fails visibly."""

from __future__ import annotations

import asyncio
import logging

import httpx
from pydantic import SecretStr

from att_gateway.login import read_page
from att_gateway.pages import parse_syslog, syslog_form
from att_gateway.settings import Syslog, SyslogSettings

logger = logging.getLogger(__name__)


async def reconcile(
    client: httpx.AsyncClient, desired: Syslog, access_code: SecretStr, page_gap_seconds: float
) -> bool:
    """Save `desired` if the gateway's setting differs; returns whether it saved."""
    page = await read_page(client, "syslog", parse_syslog, access_code, page_gap_seconds)
    if page.syslog == desired:
        logger.info("gateway syslog is %s already", desired)
        return False
    logger.info("gateway syslog is %s, saving %s", page.syslog, desired)
    await asyncio.sleep(page_gap_seconds)
    response = await client.post("/cgi-bin/syslog.ha", data=syslog_form(desired, page.nonce))
    response.raise_for_status()
    await asyncio.sleep(page_gap_seconds)
    saved = await read_page(client, "syslog", parse_syslog, access_code, page_gap_seconds)
    if saved.syslog != desired:
        raise RuntimeError(f"gateway syslog is {saved.syslog=} after saving {desired=}")
    return True


async def _main(settings: SyslogSettings) -> None:
    async with httpx.AsyncClient(base_url=str(settings.url), timeout=settings.request_timeout_seconds) as client:
        await reconcile(client, settings.syslog, settings.access_code, settings.page_gap_seconds)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main(SyslogSettings()))


if __name__ == "__main__":
    main()
