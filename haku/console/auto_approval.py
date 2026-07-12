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
UNCONDITIONAL_AUTO_APPROVAL_ID = "unconditional_v1"

# Remote (operator_oauth) server ids — must match the console config
# (`cluster/k8s/haku/console/config.yaml`).
GROCY_SF_SERVER_ID = "grocy-sf"
TANA_RW_SERVER_ID = "tana-rw"

# The reviewed read-only subset of grocy-sf's tools (get/list only — every create/edit/delete/add/
# consume/set/transfer/undo/merge/clear/upload stays approval-gated).
GROCY_READ_TOOLS = frozenset(
    {
        "entities_get",
        "entities_list",
        "file_get",
        "get_below_minimum_stock",
        "get_current_user",
        "get_db_changed_time",
        "get_expired_stock",
        "get_expiring_stock",
        "get_product_stock",
        "get_system_info",
        "list_volatile_stock",
        "locations_list",
        "product_groups_list",
        "products_list",
        "quantity_units_list",
        "shopping_list_get",
        "shopping_lists_list",
        "stock_entries_list",
        "stock_get",
    }
)
# tana-rw tools auto-approved regardless of arguments. `get_or_create_calendar_node` is idempotent
# (it just resolves/creates a date container), so it is safe to auto-allow.
TANA_AUTO_APPROVE_TOOLS = frozenset({"get_or_create_calendar_node"})

# (server_id -> tools) auto-approved for the Haku agent regardless of arguments. These are remote
# operator_oauth servers with no in-process schema, so their arguments are validated by the upstream
# at execution time (not here).
UNCONDITIONAL_AUTO_APPROVE: dict[str, frozenset[str]] = {
    GROCY_SF_SERVER_ID: GROCY_READ_TOOLS,
    TANA_RW_SERVER_ID: TANA_AUTO_APPROVE_TOOLS,
}


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

    Unconditionally allowlisted read-only/safe tools (grocy-sf reads, tana
    `get_or_create_calendar_node`) approve regardless of arguments. Gmail calls go through the
    existing boundary: the FastMCP tool's published schema is validated before the label-prefix
    semantic check. Any schema, lookup, or policy error is logged and fails closed.
    """
    if caller_principal != HAKU_AGENT_PRINCIPAL:
        return None, None
    # Remote read-only/safe allowlist (grocy-sf reads, tana get_or_create_calendar_node): these are
    # operator_oauth servers with no in-process schema, so the upstream validates arguments at
    # execution time rather than here.
    if tool_name in UNCONDITIONAL_AUTO_APPROVE.get(server_id, frozenset()):
        return UNCONDITIONAL_AUTO_APPROVAL_ID, f"approved: {server_id}/{tool_name} is allowlisted read-only/safe"
    if server_id != GMAIL_SERVER_ID:
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
