"""The reviewed, fail-closed auto-approval decision for haku-console MCP calls."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import jsonschema
from fastmcp import FastMCP

from haku.console.tools.gmail_client import GMAIL_SERVER_ID, GmailToolsClient

logger = logging.getLogger(__name__)

HAKU_AGENT_PRINCIPAL = "haku-agent-api-token"
GMAIL_AUTO_APPROVAL_ID = "v1"


async def auto_approve_tool_call(
    *,
    caller_principal: str,
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    label_prefix: str,
    gmail: GmailToolsClient | None,
    mcp: FastMCP | None,
) -> tuple[str | None, str | None]:
    """Return the approving policy ID and an audit-safe evaluation string.

    The existing FastMCP tool is the input-contract source of truth. Its published
    schema is synthesized from the callable signature and validated here before the
    Gmail-specific semantic boundary is evaluated. Any schema, lookup, or policy
    error is logged and fails closed.
    """
    if caller_principal != HAKU_AGENT_PRINCIPAL or server_id != GMAIL_SERVER_ID:
        return None, None
    if tool_name not in {
        "threads_list",
        "threads_get",
        "messages_get",
        "labels_list",
        "labels_get",
        "threads_modify_labels",
        "labels_patch",
        "labels_delete",
    }:
        return None, f"manual: Gmail tool {tool_name!r} is not auto-approved"

    try:
        if mcp is None:
            raise RuntimeError("in-process Gmail MCP server is unavailable")
        tool = await mcp.get_tool(tool_name)
        if tool is None:
            raise RuntimeError(f"Gmail MCP tool {tool_name!r} is unavailable")
    except Exception:
        logger.exception("auto-approval tool lookup failed server=%s tool=%s", server_id, tool_name)
        return None, "error: registered Gmail tool lookup failed"

    try:
        jsonschema.validate(instance=arguments, schema=tool.to_mcp_tool().inputSchema)
    except jsonschema.ValidationError as exc:
        logger.warning("auto-approval rejected invalid MCP arguments server=%s tool=%s: %s", server_id, tool_name, exc)
        return None, "manual: arguments failed the registered Gmail tool schema"
    except jsonschema.SchemaError:
        logger.exception("auto-approval tool schema is invalid server=%s tool=%s", server_id, tool_name)
        return None, "error: registered Gmail tool schema is invalid"

    try:
        if not label_prefix:
            raise ValueError("Gmail auto-approval label prefix must be non-empty")

        def allows_label(name: str) -> bool:
            return name.startswith(label_prefix)

        if tool_name in {"threads_list", "threads_get", "messages_get", "labels_list", "labels_get"}:
            return GMAIL_AUTO_APPROVAL_ID, "approved: Gmail search/read operation"
        if tool_name == "threads_modify_labels":
            add = arguments.get("add") or []
            remove = arguments.get("remove") or []
            if not add and not remove:
                return None, "manual: no label changes requested"
            if set(add) & set(remove):
                return None, "manual: a label cannot be both added and removed"
            if all(allows_label(name) for name in [*add, *remove]):
                return GMAIL_AUTO_APPROVAL_ID, f"approved: all label names are under {label_prefix!r}"
            return None, f"manual: at least one label name is outside {label_prefix!r}"
        if gmail is None:
            raise RuntimeError("Gmail client is unavailable")
        if tool_name == "labels_patch":
            new_name = arguments.get("name")
            if (
                new_name is None
                or arguments.get("label_list_visibility") is not None
                or arguments.get("message_list_visibility") is not None
            ):
                return None, "manual: label rename required without visibility changes"
            current = await asyncio.to_thread(gmail.labels_get, arguments["label_id"])
            if allows_label(current.name) and allows_label(new_name):
                return GMAIL_AUTO_APPROVAL_ID, f"approved: current and new label names are under {label_prefix!r}"
            return None, f"manual: current or new label name is outside {label_prefix!r}"
        if tool_name == "labels_delete":
            current = await asyncio.to_thread(gmail.labels_get, arguments["label_id"])
            if allows_label(current.name):
                return GMAIL_AUTO_APPROVAL_ID, f"approved: label name is under {label_prefix!r}"
            return None, f"manual: label name is outside {label_prefix!r}"
        return None, "manual: Gmail operation did not match an auto-approval rule"
    except Exception:
        logger.exception("auto-approval evaluation failed server=%s tool=%s", server_id, tool_name)
    return None, "error: Gmail auto-approval evaluation failed"
