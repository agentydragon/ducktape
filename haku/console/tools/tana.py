"""Tana node-name lookup for haku-console approval previews."""

from __future__ import annotations

import asyncio
import re
from typing import Annotated, Protocol

from fastapi import APIRouter, Query, Request
from fastmcp.client.client import CallToolResult as FastMCPCallToolResult
from mcp import types as mcp_types
from pydantic import BaseModel

from haku.console.deps import SettingsDep
from haku.console.mcp_approval import operator_authenticated_client
from haku.console.mcp_operator_oauth import OAuthStoreDep

TANA_RW_SERVER_ID = "tana-rw"

router = APIRouter(prefix="/api/tana-rw", tags=["tana"])


class TanaNodePreview(BaseModel):
    id: str
    name: str


class TanaNodePreviewsResponse(BaseModel):
    nodes: list[TanaNodePreview]


class _ToolClient(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, object]) -> FastMCPCallToolResult: ...


_NODE_MARKER_RE = re.compile(r"^(?P<name>.*?)\s*<!-- node-id: (?P<id>[^ ]+) -->\s*$", re.MULTILINE)
_BULLET_PREFIX_RE = re.compile(r"^\s*(?:-\s+)?(?:\[[ Xx]\]\s+)?")


def node_name_from_markdown(markdown: str, node_id: str) -> str | None:
    """Extract a node's displayed name from Tana's read_node markdown response."""
    for match in _NODE_MARKER_RE.finditer(markdown):
        if match["id"] == node_id:
            name = _BULLET_PREFIX_RE.sub("", match["name"]).strip()
            return name or None
    return None


def _tool_result_text(result: FastMCPCallToolResult) -> str | None:
    for content in result.content:
        if isinstance(content, mcp_types.TextContent):
            return content.text
    return None


async def _read_node_preview(client: _ToolClient, node_id: str) -> TanaNodePreview | None:
    try:
        result = await client.call_tool("read_node", {"nodeId": node_id, "maxDepth": 0})
    except Exception:
        return None
    markdown = _tool_result_text(result)
    if markdown is None:
        return None
    name = node_name_from_markdown(markdown, node_id)
    return TanaNodePreview(id=node_id, name=name) if name is not None else None


@router.get("/node-previews")
async def tana_node_previews(
    request: Request, settings: SettingsDep, oauth_store: OAuthStoreDep, node_id: Annotated[list[str], Query()]
) -> TanaNodePreviewsResponse:
    """Resolve Tana node IDs for preview display through the approving operator's account."""
    node_ids = list(dict.fromkeys(node_id))
    async with await operator_authenticated_client(TANA_RW_SERVER_ID, request, settings, oauth_store) as client:
        previews = await asyncio.gather(*(_read_node_preview(client, id_) for id_ in node_ids))
    return TanaNodePreviewsResponse(nodes=[preview for preview in previews if preview is not None])
