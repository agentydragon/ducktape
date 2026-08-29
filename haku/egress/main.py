"""Entry point for the colocated egress proxy sidecar (#4942).

Runs the embedded mitmproxy adapter (``EgressProxy``) beside the Console pod and
points it at Console's loopback-bound decision endpoint through
``LocalhostDecideClient``. The oracle is reachable only over the shared pod
loopback (``127.0.0.1``); no sandbox workload has a route to it, so sandbox
unreachability is structural rather than a NetworkPolicy (#4670 § Topology,
acceptance criterion 14).

Configuration is entirely environment-driven so the same image serves every
deploy:

- ``HAKU_EGRESS_DECIDE_URL`` — base URL of Console's loopback decide listener,
  e.g. ``http://127.0.0.1:8079``.
- ``HAKU_EGRESS_FENCE_CREDENTIAL`` — the shared-fence credential this proxy presents
  in the ``Authorization`` header to Console. It authenticates the shared fence only;
  it is not the sandbox-to-proxy credential or an Agent identity.
- ``HAKU_EGRESS_CONFDIR`` — mitmproxy confdir holding the shared interception CA
  (``mitmproxy-ca.pem``); an init container assembles it from the deploy CA.
- ``HAKU_EGRESS_LISTEN_HOST`` / ``HAKU_EGRESS_LISTEN_PORT`` — the fenced-workload
  facing proxy listener (default ``0.0.0.0:8888``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

from pydantic import SecretStr

from haku.egress.localhost_decide_client import LocalhostDecideClient
from haku.egress.runner import EgressProxy

logger = logging.getLogger(__name__)


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"missing required environment variable {name}")
    return value


async def async_main() -> None:
    decide = LocalhostDecideClient(
        base_url=_require("HAKU_EGRESS_DECIDE_URL"),
        fence_credential=SecretStr(_require("HAKU_EGRESS_FENCE_CREDENTIAL")),
    )
    confdir = Path(_require("HAKU_EGRESS_CONFDIR"))
    listen_host = os.environ.get("HAKU_EGRESS_LISTEN_HOST", "0.0.0.0")
    listen_port = int(os.environ.get("HAKU_EGRESS_LISTEN_PORT", "8888"))
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        async with EgressProxy(decide, confdir=confdir, listen_host=listen_host, listen_port=listen_port):
            await stop.wait()
    finally:
        await decide.aclose()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
