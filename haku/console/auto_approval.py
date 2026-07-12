"""The reviewed, fail-closed auto-approval decision for haku-console MCP calls."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import jsonschema
from fastmcp import FastMCP

from haku.console.tools.gmail_client import GMAIL_SERVER_ID, GmailToolsClient

logger = logging.getLogger(__name__)

GMAIL_AUTO_APPROVAL_ID = "gmail_labels_v1"
UNCONDITIONAL_AUTO_APPROVAL_ID = "unconditional_v1"

# Remote (operator_oauth) server ids — must match the console config
# (`cluster/k8s/haku/console/config.yaml`). Kept as literals here (rather than imported from
# `haku.console.tools.{grocy,tana}`) to avoid an import cycle through `mcp_approval`.
GROCY_SF_SERVER_ID = "grocy-sf"
TANA_RW_SERVER_ID = "tana-rw"

# Gmail read tools auto-approved for any authenticated agent regardless of arguments.
GMAIL_READ_TOOLS = frozenset({"threads_list", "threads_get", "messages_get", "labels_list", "labels_get"})
# Gmail mutations that may auto-approve depending on arguments (haku/-prefixed labels).
GMAIL_CONDITIONAL_TOOLS = frozenset({"threads_modify_labels", "labels_patch", "labels_delete"})

# The reviewed read-only subset of grocy-sf's tools (get/list only — every create/edit/delete/
# add/consume/set/transfer/undo/merge/clear/upload stays approval-gated).
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

# (server_id -> tools) auto-approved for any authenticated agent regardless of arguments. Drives the
# MCP server's transparent pass-through bucket. Argument-conditional approvals
# (GMAIL_CONDITIONAL_TOOLS) are deliberately excluded — those still route through the request_
# envelope and auto-approve per call.
UNCONDITIONAL_AUTO_APPROVE: dict[str, frozenset[str]] = {
    GMAIL_SERVER_ID: GMAIL_READ_TOOLS,
    GROCY_SF_SERVER_ID: GROCY_READ_TOOLS,
    TANA_RW_SERVER_ID: TANA_AUTO_APPROVE_TOOLS,
}


def is_unconditionally_auto_approved(server_id: str, tool_name: str) -> bool:
    """Whether every valid call to this tool auto-approves for an authenticated agent."""
    return tool_name in UNCONDITIONAL_AUTO_APPROVE.get(server_id, frozenset())


async def _validate_arguments(mcp: FastMCP, tool_name: str, arguments: dict[str, Any]) -> str | None:
    """Validate arguments against the in-process tool's generated schema.

    Returns an audit-safe error string on rejection, or None if valid. Lookup / schema errors are
    logged and fail closed (rejected). Only usable for in-process servers (gmail/google_calendar);
    remote servers have no in-process schema and are validated by the upstream at execution time.
    """
    try:
        tool = await mcp.get_tool(tool_name)
        if tool is None:
            raise RuntimeError(f"tool {tool_name!r} is unavailable")
    except Exception:
        logger.exception("auto-approval tool lookup failed tool=%s", tool_name)
        return "error: registered tool lookup failed"
    try:
        jsonschema.validate(instance=arguments, schema=tool.to_mcp_tool().inputSchema)
    except jsonschema.ValidationError as exc:
        logger.warning("auto-approval rejected invalid MCP arguments tool=%s: %s", tool_name, exc)
        return "manual: arguments failed the registered tool schema"
    except jsonschema.SchemaError:
        logger.exception("auto-approval tool schema is invalid tool=%s", tool_name)
        return "error: registered tool schema is invalid"
    return None


async def auto_approve_tool_call(
    *,
    caller_is_agent: bool,
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    label_prefix: str,
    gmail: GmailToolsClient | None,
    mcp: FastMCP | None,
) -> tuple[str | None, str | None]:
    """Return the approving policy ID and an audit-safe evaluation string.

    Applies to any authenticated agent (the machine API token or an MCP OAuth client); interactive
    operator-browser callers pass ``caller_is_agent=False`` and never auto-approve. Unconditionally
    allowlisted read-only/safe operations (gmail reads, grocy-sf reads, tana `get_or_create_calendar_node`)
    approve regardless of arguments; gmail label mutations approve only when scoped to ``label_prefix``.
    Any schema, lookup, or policy error is logged and fails closed.
    """
    if not caller_is_agent:
        return None, None
    if is_unconditionally_auto_approved(server_id, tool_name):
        # In-process servers (gmail/google_calendar) expose their schema here, so validate; remote
        # servers (grocy-sf/tana-rw) validate at execution.
        if mcp is not None:
            error = await _validate_arguments(mcp, tool_name, arguments)
            if error is not None:
                return None, error
        return UNCONDITIONAL_AUTO_APPROVAL_ID, f"approved: {server_id}/{tool_name} is allowlisted read-only/safe"
    if server_id == GMAIL_SERVER_ID and tool_name in GMAIL_CONDITIONAL_TOOLS:
        return await _approve_gmail_label_op(tool_name, arguments, label_prefix, gmail, mcp)
    return None, f"manual: {server_id}/{tool_name} is not auto-approved"


async def _approve_gmail_label_op(
    tool_name: str, arguments: dict[str, Any], label_prefix: str, gmail: GmailToolsClient | None, mcp: FastMCP | None
) -> tuple[str | None, str | None]:
    """The reviewed gmail label-mutation boundary: approve only haku/-prefixed label changes."""
    if mcp is None:
        logger.error("gmail auto-approval: in-process Gmail server unavailable")
        return None, "error: in-process Gmail server unavailable"
    error = await _validate_arguments(mcp, tool_name, arguments)
    if error is not None:
        return None, error
    try:
        if not label_prefix:
            raise ValueError("Gmail auto-approval label prefix must be non-empty")

        def allows_label(name: str) -> bool:
            return name.startswith(label_prefix)

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
        logger.exception("auto-approval evaluation failed tool=%s", tool_name)
    return None, "error: Gmail auto-approval evaluation failed"
