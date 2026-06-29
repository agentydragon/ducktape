"""FastMCP server exposing namespace-scoped Gmail label management.

The whole tool surface is closed over `allowed_prefix` (enforced in `LabelClient`),
so the server is safe to attach to an autonomous agent without a human-in-the-loop
gate. Cluster-internal access is gated by a static bearer (see `StaticBearerGuard`).
"""

import asyncio
import logging
import os
import sys
from typing import Annotated

import uvicorn
from fastmcp import FastMCP
from pydantic import Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.types import ASGIApp

from gmail_api.service import build_gmail_service_from_token_dir
from haku.gmail_labeling.backend import GmailLabelBackend
from haku.gmail_labeling.client import LabelClient
from haku.gmail_labeling.config import Settings
from haku.gmail_labeling.models import Label
from haku.gmail_labeling.namespace import LabelNamespace
from mcp_infra.static_bearer import StaticBearerGuard

logger = logging.getLogger(__name__)

INSTRUCTIONS = (
    "Manage Gmail labels confined to a configured namespace prefix (e.g. 'haku/'). "
    "Every label name must start with that prefix; the server refuses anything else, "
    "so you cannot touch system labels (INBOX/TRASH/SPAM/…) or the user's other labels."
)

ThreadId = Annotated[str, Field(description="Gmail thread ID to label (from Gmail search results).")]
LabelName = Annotated[
    str, Field(description="Label display name; must start with the managed prefix, e.g. 'haku/triaged'.")
]


def build_mcp(client: LabelClient) -> FastMCP:
    mcp: FastMCP = FastMCP(name="gmail-labeling", instructions=INSTRUCTIONS)

    @mcp.tool
    async def list_labels() -> list[Label]:
        """List the labels managed by this server (those under the namespace prefix)."""
        return await asyncio.to_thread(client.list_labels)

    @mcp.tool
    async def apply_label(thread_id: ThreadId, name: LabelName) -> Label:
        """Add a managed label to a thread, creating the label if it does not exist yet."""
        return await asyncio.to_thread(client.apply_label, thread_id, name)

    @mcp.tool
    async def remove_label(thread_id: ThreadId, name: LabelName) -> Label:
        """Remove a managed label from a thread."""
        return await asyncio.to_thread(client.remove_label, thread_id, name)

    @mcp.tool
    async def create_label(name: LabelName) -> Label:
        """Create a managed label without applying it to any thread."""
        return await asyncio.to_thread(client.create_label, name)

    @mcp.tool
    async def rename_label(
        old: Annotated[str, Field(description="Current label name; must be under the managed prefix.")],
        new: Annotated[str, Field(description="New label name; must also be under the managed prefix.")],
    ) -> Label:
        """Rename a managed label. Both the old and new name must be under the namespace prefix."""
        return await asyncio.to_thread(client.rename_label, old, new)

    @mcp.tool
    async def delete_label(name: LabelName) -> None:
        """Delete a managed label (removes it from every thread it is on)."""
        await asyncio.to_thread(client.delete_label, name)

    return mcp


def build_app(settings: Settings) -> Starlette:
    service = build_gmail_service_from_token_dir(settings.gmail_token_dir)
    client = LabelClient(GmailLabelBackend(service), LabelNamespace(settings.allowed_prefix))
    mcp_app = build_mcp(client).http_app(path="/mcp")

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    mounted: ASGIApp = mcp_app
    if settings.static_bearer:
        mounted = StaticBearerGuard(mcp_app, token=settings.static_bearer)
    else:
        logger.warning("no static_bearer configured — /mcp is unauthenticated (local/dev only)")
    return Starlette(routes=[Route("/healthz", healthz), Mount("/", app=mounted)], lifespan=mcp_app.lifespan)


def main() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=log_level, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    settings = Settings()
    app = build_app(settings)
    logger.info("gmail-labeling listening on %s:%d (prefix=%r)", settings.host, settings.port, settings.allowed_prefix)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
