"""FastMCP server exposing namespace-scoped Gmail label management.

The whole tool surface is closed over `allowed_prefix` (enforced in `LabelClient`),
so the server is safe to attach to an autonomous agent without a human-in-the-loop
gate. `/mcp` accepts two credentials on one endpoint: a static bearer (Haku's machine
path) and, when configured, an Authentik OAuth flow (an interactive operator, e.g.
claude.ai) — both ride one FastMCP `MultiAuth` (see `build_app`).
"""

import asyncio
import logging
import os
import sys
from typing import Annotated

import uvicorn
from fastmcp import FastMCP
from fastmcp.server.auth import MultiAuth
from fastmcp.server.auth.providers.jwt import StaticTokenVerifier
from pydantic import Field
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from gmail_api.service import build_gmail_service_from_token_dir
from haku.gmail_labeling.backend import GmailLabelBackend
from haku.gmail_labeling.client import LabelClient
from haku.gmail_labeling.config import Settings
from haku.gmail_labeling.models import Label
from haku.gmail_labeling.namespace import LabelNamespace
from mcp_infra.authentik_auth.auth import build_authentik_auth
from mcp_infra.persistence import build_client_storage

logger = logging.getLogger(__name__)


def build_mcp(client: LabelClient) -> FastMCP:
    prefix = client.prefix
    instructions = (
        f"Manage Gmail labels confined to the {prefix!r} namespace. Every label name must start "
        f"with {prefix!r}; the server refuses anything else, so you cannot touch system labels "
        "(INBOX/TRASH/SPAM/…) or the user's other labels."
    )
    thread_id_ann = Annotated[str, Field(description="Gmail thread ID to label (from Gmail search results).")]
    label_name_ann = Annotated[
        str, Field(description=f"Label display name; must start with {prefix!r}, e.g. {prefix + 'triaged'!r}.")
    ]

    mcp: FastMCP = FastMCP(name="gmail-labeling", instructions=instructions)

    @mcp.tool
    async def list_labels() -> list[Label]:
        """List the labels managed by this server (those under the namespace prefix)."""
        return await asyncio.to_thread(client.list_labels)

    @mcp.tool
    async def apply_label(thread_id: thread_id_ann, name: label_name_ann) -> Label:
        """Add a managed label to a thread, creating the label if it does not exist yet."""
        return await asyncio.to_thread(client.apply_label, thread_id, name)

    @mcp.tool
    async def remove_label(thread_id: thread_id_ann, name: label_name_ann) -> Label:
        """Remove a managed label from a thread."""
        return await asyncio.to_thread(client.remove_label, thread_id, name)

    @mcp.tool
    async def create_label(name: label_name_ann) -> Label:
        """Create a managed label without applying it to any thread."""
        return await asyncio.to_thread(client.create_label, name)

    @mcp.tool
    async def rename_label(
        old: Annotated[str, Field(description=f"Current label name; must be under {prefix!r}.")],
        new: Annotated[str, Field(description=f"New label name; must also be under {prefix!r}.")],
    ) -> Label:
        """Rename a managed label. Both the old and new name must be under the namespace prefix."""
        return await asyncio.to_thread(client.rename_label, old, new)

    @mcp.tool
    async def delete_label(name: label_name_ann) -> None:
        """Delete a managed label (removes it from every thread it is on)."""
        await asyncio.to_thread(client.delete_label, name)

    return mcp


def build_app(settings: Settings) -> Starlette:
    service = build_gmail_service_from_token_dir(settings.gmail_token_dir)
    client = LabelClient(GmailLabelBackend(service), LabelNamespace(settings.allowed_prefix))
    mcp = build_mcp(client)

    # One endpoint, up to two accepted credentials, always composed with FastMCP's
    # MultiAuth. MultiAuth is asymmetric: one `server` owns the OAuth routes/metadata
    # (the Authentik OIDCProxy) and the `verifiers` are token validators tried after
    # it — so Haku's static bearer rides as a verifier alongside the OAuth flow (there
    # can only be one OAuth server). With no Authentik config, the static bearer is the
    # sole verifier and there's no OAuth flow.
    verifiers = [StaticTokenVerifier({settings.static_bearer: {"client_id": "haku"}})] if settings.static_bearer else []
    if settings.authentik:
        mcp.auth = build_authentik_auth(
            settings.authentik, client_storage=build_client_storage(settings.persistence), extra_verifiers=verifiers
        )
    elif verifiers:
        mcp.auth = MultiAuth(verifiers=verifiers)
    else:
        logger.warning("no auth configured — /mcp is unauthenticated (local/dev only)")

    mcp_app = mcp.http_app(path="/mcp")

    async def healthz(request: Request) -> JSONResponse:
        return JSONResponse({"ok": True})

    return Starlette(routes=[Route("/healthz", healthz), Mount("/", app=mcp_app)], lifespan=mcp_app.lifespan)


def main() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=log_level, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
    settings = Settings()
    app = build_app(settings)
    logger.info("gmail-labeling listening on %s:%d (prefix=%r)", settings.host, settings.port, settings.allowed_prefix)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
