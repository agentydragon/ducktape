"""The reviewed, fail-closed auto-approval decision for haku-console MCP calls."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import jsonschema
from fastmcp import FastMCP

from haku.console.tools.gmail_client import GmailToolsClient
from haku.gmail_labeling.namespace import LabelNamespace

logger = logging.getLogger(__name__)

HAKU_AGENT_PRINCIPAL = "haku-agent-api-token"
GMAIL_SERVER_ID = "gmail"
GMAIL_AUTO_APPROVAL_ID = "gmail.read_and_haku_labels.v1"


async def auto_approve_tool_call(
    *,
    caller_principal: str,
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    label_prefix: str,
    gmail: GmailToolsClient | None,
    mcp: FastMCP | None,
) -> str | None:
    """Return the approving policy ID, or ``None`` for ordinary manual approval.

    The existing FastMCP tool is the input-contract source of truth. Its published
    schema is synthesized from the callable signature and validated here before the
    Gmail-specific semantic boundary is evaluated. Any schema, lookup, or policy
    error is logged and fails closed.
    """
    if caller_principal != HAKU_AGENT_PRINCIPAL or server_id != GMAIL_SERVER_ID:
        return None
    if tool_name not in {
        "threads_list",
        "threads_get",
        "messages_get",
        "labels_list",
        "labels_get",
        "threads_batch_modify",
        "labels_patch",
        "labels_delete",
    }:
        return None

    try:
        if mcp is None:
            raise RuntimeError("in-process Gmail MCP server is unavailable")
        tool = await mcp.get_tool(tool_name)
        if tool is None:
            raise RuntimeError(f"Gmail MCP tool {tool_name!r} is unavailable")
    except Exception:
        logger.exception("auto-approval tool lookup failed server=%s tool=%s", server_id, tool_name)
        return None

    try:
        jsonschema.validate(instance=arguments, schema=tool.to_mcp_tool().inputSchema)
    except jsonschema.ValidationError as exc:
        logger.warning("auto-approval rejected invalid MCP arguments server=%s tool=%s: %s", server_id, tool_name, exc)
        return None
    except jsonschema.SchemaError:
        logger.exception("auto-approval tool schema is invalid server=%s tool=%s", server_id, tool_name)
        return None

    try:
        namespace = LabelNamespace(label_prefix)
        if tool_name in {"threads_list", "threads_get", "messages_get", "labels_list", "labels_get"}:
            return GMAIL_AUTO_APPROVAL_ID
        if tool_name == "threads_batch_modify":
            add = arguments.get("add") or []
            remove = arguments.get("remove") or []
            if not add and not remove:
                return None
            if set(add) & set(remove):
                return None
            return GMAIL_AUTO_APPROVAL_ID if all(namespace.allows(name) for name in [*add, *remove]) else None
        if gmail is None:
            raise RuntimeError("Gmail client is unavailable")
        if tool_name == "labels_patch":
            new_name = arguments.get("name")
            if (
                new_name is None
                or arguments.get("label_list_visibility") is not None
                or arguments.get("message_list_visibility") is not None
            ):
                return None
            current = await asyncio.to_thread(gmail.labels_get, arguments["label_id"])
            return GMAIL_AUTO_APPROVAL_ID if namespace.allows(current.name) and namespace.allows(new_name) else None
        current = await asyncio.to_thread(gmail.labels_get, arguments["label_id"])
        return GMAIL_AUTO_APPROVAL_ID if namespace.allows(current.name) else None
    except Exception:
        logger.exception("auto-approval evaluation failed server=%s tool=%s", server_id, tool_name)
    return None
