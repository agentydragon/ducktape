"""Run the date-clamping wayback proxy as an embedded mitmproxy.

mitmproxy handles CONNECT, TLS interception, and per-host cert minting from its
CA; the :class:`WaybackAddon` answers every flow from the clamped archive. The
CA cert lives at ``<confdir>/mitmproxy-ca-cert.pem`` (generated on first run);
mount that into the agent's trust store so ``https://`` requests validate.

Manifest evidence lines go to stdout; mitmproxy/diagnostic logs go to stderr.
See loom/plans/wayback_proxy.md and README.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import aiohttp
from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster

from loom.wayback_proxy.addon import WaybackAddon
from loom.wayback_proxy.proxy import Config, WaybackResolver

logger = logging.getLogger(__name__)


def confdir() -> Path:
    """mitmproxy CA directory; override for a CA shared with the agent's trust store."""
    return Path(os.environ.get("WAYBACK_CONFDIR", str(Path.home() / ".mitmproxy")))


async def amain() -> None:
    config = Config.from_env()
    ca_dir = confdir()
    ca_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "wayback proxy: as_of=%s upstream=%s port=%d confdir=%s", config.as_of, config.upstream, config.port, ca_dir
    )
    # Total timeout rides out wayback-cache limit_req delays (burst 60 @ 30r/m).
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as session:
        resolver = WaybackResolver(config, session, sys.stdout)
        options = Options(listen_host="0.0.0.0", listen_port=config.port, confdir=str(ca_dir))
        master = DumpMaster(options, with_termlog=False, with_dumper=False)
        master.addons.add(WaybackAddon(resolver))
        await master.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    asyncio.run(amain())


if __name__ == "__main__":
    main()
