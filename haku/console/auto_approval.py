"""The reviewed, fail-closed auto-approval decision for haku-console MCP calls."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import jsonschema
from fastmcp import FastMCP

from haku.console.tool_call_actor import AgentActor, ToolCallActor
from haku.console.tools.gmail_client import GMAIL_SERVER_ID, GmailToolsClient

logger = logging.getLogger(__name__)

GMAIL_AUTO_APPROVAL_ID = "gmail_labels_v1"
UNCONDITIONAL_AUTO_APPROVAL_ID = "unconditional_v1"
SCHEMA_AUTO_DENIAL_EVALUATION = "denied: arguments failed the registered tool schema"


@dataclass(frozen=True)
class SchemaDenial:
    """Arguments failed an owned in-process tool's schema — the call is terminally auto-denied.

    Only in-process servers reach this decision (they are the only ones whose registered schema
    the console can vouch for; remote servers validate upstream at execution). A call that can
    never execute must fail fast to the caller with the validation error — not consume
    approval-queue attention (operator directive 2026-07-16).
    """

    reason: str  # caller-facing denial reason: the concrete validation error
    evaluation: str = SCHEMA_AUTO_DENIAL_EVALUATION  # audit string recorded on the row


# Remote (operator_oauth) server ids — must match the console config
# (`cluster/k8s/haku/console/config.yaml`). Kept as literals here (rather than imported from
# `haku.console.tools.{grocy,tana}`) to avoid an import cycle through `mcp_approval`.
GROCY_SF_SERVER_ID = "grocy-sf"
TANA_RW_SERVER_ID = "tana-rw"
GOOGLE_CALENDAR_SERVER_ID = "google_calendar"
IBKR_SERVER_ID = "interactive_brokers"
OSM_SERVER_ID = "osm"

# Gmail read tools auto-approved for any authenticated agent regardless of arguments.
GMAIL_READ_TOOLS = frozenset(
    {
        "threads_list",
        "threads_get",
        "messages_get",
        "labels_list",
        "labels_get",
        "filters_list",
        "filters_get",
        "drafts_list",
        "drafts_get",
    }
)
# Gmail mutations that may auto-approve depending on arguments (haku/-prefixed labels).
GMAIL_CONDITIONAL_TOOLS = frozenset({"threads_modify_labels", "labels_patch", "labels_delete"})

# Calendar reads expose operator-owned event data but cannot modify it; authenticated agents receive
# the same standing read authority as the existing Gmail and Grocy read tools.
GOOGLE_CALENDAR_READ_TOOLS = frozenset({"get_event", "list_events", "list_event_instances"})

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

# ibkr's entire surface is read-only by construction — the server reflects no order/trade routes
# (ibkr_mcp/route_policy.py), so every tool auto-approves. The market-data/secdef/scanner tools and
# `session_status` are pure reads; `request_reauth` only fires the IBKR Mobile 2FA push, which does
# nothing without the operator's phone tap.
IBKR_AUTO_APPROVE_TOOLS = frozenset(
    {
        "market_data_snapshot",
        "market_data_history",
        "secdef_search",
        "secdef_info",
        "secdef_strikes",
        "contract_info",
        "scanner_params",
        "scanner_run",
        "session_status",
        "request_reauth",
    }
)

# osmmcp's entire surface is read-only queries over public OSM data (geocoding, routing, place
# lookup, coordinate/polyline utilities) — no create/update/delete tool exists, so every tool
# auto-approves. `tile_cache`'s only actions are list/get/stats (no invalidate/clear).
OSM_AUTO_APPROVE_TOOLS = frozenset(
    {
        "get_version",
        "geocode_address",
        "reverse_geocode",
        "get_map_image",
        "route_fetch",
        "get_route_directions",
        "suggest_meeting_point",
        "route_sample",
        "analyze_commute",
        "find_nearby_places",
        "explore_area",
        "find_parking_facilities",
        "find_charging_stations",
        "find_schools_nearby",
        "analyze_neighborhood",
        "geo_distance",
        "bbox_from_points",
        "centroid_points",
        "polyline_decode",
        "polyline_encode",
        "enrich_emissions",
        "osm_query_bbox",
        "filter_tags",
        "sort_by_distance",
        "tile_cache",
    }
)

# (server_id -> tools) auto-approved for any authenticated agent regardless of arguments. Drives the
# MCP server's transparent pass-through bucket. Argument-conditional approvals
# (GMAIL_CONDITIONAL_TOOLS) are deliberately excluded — those still route through the request_
# envelope and auto-approve per call.
UNCONDITIONAL_AUTO_APPROVE: dict[str, frozenset[str]] = {
    GMAIL_SERVER_ID: GMAIL_READ_TOOLS,
    GOOGLE_CALENDAR_SERVER_ID: GOOGLE_CALENDAR_READ_TOOLS,
    GROCY_SF_SERVER_ID: GROCY_READ_TOOLS,
    TANA_RW_SERVER_ID: TANA_AUTO_APPROVE_TOOLS,
    IBKR_SERVER_ID: IBKR_AUTO_APPROVE_TOOLS,
    OSM_SERVER_ID: OSM_AUTO_APPROVE_TOOLS,
}


def is_unconditionally_auto_approved(server_id: str, tool_name: str) -> bool:
    """Whether every valid call to this tool auto-approves for an authenticated agent."""
    return tool_name in UNCONDITIONAL_AUTO_APPROVE.get(server_id, frozenset())


async def _validate_arguments(mcp: FastMCP, tool_name: str, arguments: dict[str, Any]) -> SchemaDenial | str | None:
    """Validate arguments against the in-process tool's generated schema.

    Returns None if valid; a `SchemaDenial` when the *arguments* are invalid (terminal — the call
    can never execute, so it auto-denies instead of queueing); or an audit-safe error string when
    the console itself failed to look up or parse the schema — that is a console-side malfunction,
    not a caller error, so it fails closed to manual review. Only usable for in-process servers
    (gmail/google_calendar); remote servers have no in-process schema and are validated by the
    upstream at execution time.
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
        logger.warning("auto-denied invalid MCP arguments tool=%s: %s", tool_name, exc)
        return SchemaDenial(reason=f"arguments failed the registered tool schema: {exc.message}")
    except jsonschema.SchemaError:
        logger.exception("auto-approval tool schema is invalid tool=%s", tool_name)
        return "error: registered tool schema is invalid"
    return None


async def auto_approve_tool_call(
    *,
    actor: ToolCallActor,
    server_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    label_prefix: str,
    gmail: GmailToolsClient | None,
    mcp: FastMCP | None,
) -> tuple[str | None, str | None] | SchemaDenial:
    """Return the approving policy ID and an audit-safe evaluation string, or a `SchemaDenial`.

    Applies to any authenticated agent (a static MCP bearer or an MCP OAuth client); interactive
    operator-browser calls never auto-approve. Unconditionally
    allowlisted read-only/safe operations (Gmail/Calendar/Grocy reads, tana `get_or_create_calendar_node`)
    approve regardless of arguments; gmail label mutations approve only when scoped to ``label_prefix``.
    Arguments that fail an owned in-process schema return `SchemaDenial` (terminal auto-denial);
    any console-side schema, lookup, or policy error is logged and fails closed to manual review.
    """
    if not isinstance(actor, AgentActor):
        return None, None
    if is_unconditionally_auto_approved(server_id, tool_name):
        # In-process servers (gmail/google_calendar) expose their schema here, so validate; remote
        # servers (grocy-sf/tana-rw) validate at execution.
        if mcp is not None:
            error = await _validate_arguments(mcp, tool_name, arguments)
            if isinstance(error, SchemaDenial):
                return error
            if error is not None:
                return None, error
        return UNCONDITIONAL_AUTO_APPROVAL_ID, f"approved: {server_id}/{tool_name} is allowlisted read-only/safe"
    if server_id == GMAIL_SERVER_ID and tool_name in GMAIL_CONDITIONAL_TOOLS:
        return await _approve_gmail_label_op(tool_name, arguments, label_prefix, gmail, mcp)
    return None, f"manual: {server_id}/{tool_name} is not auto-approved"


async def _approve_gmail_label_op(
    tool_name: str, arguments: dict[str, Any], label_prefix: str, gmail: GmailToolsClient | None, mcp: FastMCP | None
) -> tuple[str | None, str | None] | SchemaDenial:
    """The reviewed gmail label-mutation boundary: approve only haku/-prefixed label changes."""
    if mcp is None:
        logger.error("gmail auto-approval: in-process Gmail server unavailable")
        return None, "error: in-process Gmail server unavailable"
    error = await _validate_arguments(mcp, tool_name, arguments)
    if isinstance(error, SchemaDenial):
        return error
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
