"""Whoami backend — the service sitting behind the Authentik proxy outpost.

Trusts the outpost entirely: reads the X-Authentik-{Username,Email,Groups}
headers that the proxy provider injects after successful JWT validation, and
echoes them back. **Does not re-validate the JWT** — that's the outpost's job,
and doing it here would create two independent trust anchors for no benefit.

The direct connection (bypassing the outpost) is reachable only from inside
the cluster via the internal Service, and in production would be locked down
to the outpost Pod via a CiliumNetworkPolicy. For the POC we rely on the
fact that the outpost sits in front of the external hostname.
"""

from __future__ import annotations

import logging
import sys
from typing import Annotated

import uvicorn
from fastapi import FastAPI, Header

from x.authentik_mcp_poc.config import BackendSettings

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    app = FastAPI(title="Authentik MCP POC — whoami backend", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/whoami")
    async def whoami(
        x_authentik_username: Annotated[str | None, Header()] = None,
        x_authentik_email: Annotated[str | None, Header()] = None,
        x_authentik_groups: Annotated[str | None, Header()] = None,
        x_authentik_uid: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        # Groups arrive as a `|`-separated string from the Authentik outpost.
        groups = x_authentik_groups.split("|") if x_authentik_groups else []
        return {
            "user": x_authentik_username,
            "email": x_authentik_email,
            "uid": x_authentik_uid,
            "groups": groups,
            "secret_message": "auth flowed through the Authentik proxy outpost",
        }

    return app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    settings = BackendSettings()
    logger.info("whoami backend listening on %s:%d", settings.host, settings.port)
    uvicorn.run(create_app(), host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
