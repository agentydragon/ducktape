"""Run the date-clamping wayback proxy as an embedded mitmproxy.

mitmproxy handles CONNECT, TLS interception, and per-host cert minting from its
CA; the :class:`WaybackAddon` answers every flow from the clamped archive. The
CA cert lives at ``<confdir>/mitmproxy-ca-cert.pem`` (generated on first run);
mount that into the agent's trust store so ``https://`` requests validate.

Manifest evidence lines go to ``WAYBACK_MANIFEST_PATH`` (or stdout); mitmproxy
and diagnostic logs go to stderr. See loom/plans/wayback_proxy.md and README.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from contextlib import nullcontext
from pathlib import Path
from typing import TextIO

import aiohttp
from mitmproxy.options import Options
from mitmproxy.tools.dump import DumpMaster

from loom.wayback_proxy.addon import WaybackAddon
from loom.wayback_proxy.proxy import Config, WaybackResolver

logger = logging.getLogger(__name__)


def confdir() -> Path:
    """mitmproxy CA directory; override for a CA shared with the agent's trust store."""
    return Path(os.environ.get("WAYBACK_CONFDIR", str(Path.home() / ".mitmproxy")))


async def amain(config: Config, manifest: TextIO) -> None:
    ca_dir = confdir()
    ca_dir.mkdir(parents=True, exist_ok=True)
    logger.info(
        "wayback proxy: as_of=%s upstream=%s port=%d confdir=%s", config.as_of, config.upstream, config.port, ca_dir
    )
    # Authorization rides on every request; the proxy only ever GETs `upstream`
    # (off-archive redirects are returned to the client, never followed).
    headers = {"Authorization": config.upstream_auth} if config.upstream_auth is not None else {}
    # Total timeout rides out wayback-cache limit_req delays (burst 60 @ 30r/m).
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300), headers=headers) as session:
        resolver = WaybackResolver(config, session, manifest)
        options = Options(listen_host="0.0.0.0", listen_port=config.port, confdir=str(ca_dir))
        master = DumpMaster(options, with_termlog=False, with_dumper=False)
        master.addons.add(WaybackAddon(resolver))
        await master.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    config = Config.from_env()
    # The manifest file opens in sync main: blocking open in async code is an
    # antipattern, and line buffering keeps records readable mid-run.
    manifest_cm = (
        nullcontext(sys.stdout) if config.manifest_path is None else config.manifest_path.open("a", buffering=1)
    )
    with manifest_cm as manifest:
        asyncio.run(amain(config, manifest))


if __name__ == "__main__":
    main()
