from __future__ import annotations

from adgn.mcp._shared.constants import (
    APPROVAL_POLICY_PROPOSALS_INDEX_URI,
    APPROVAL_POLICY_RESOURCE_URI,
    COMPOSITOR_META_CAPABILITIES_URI_FMT,
    COMPOSITOR_META_INSTRUCTIONS_URI_FMT,
    COMPOSITOR_META_SERVER_NAME,
    COMPOSITOR_META_STATE_URI_FMT,
    RESOURCES_SUBSCRIPTIONS_INDEX_URI,
)

"""Helpers for building common MCP resource URIs.

Centralizes string construction to avoid ad-hoc concatenations across modules.
"""

# ---- Approval policy ----


def approval_policy_uri() -> str:
    """Canonical resource URI for the active approval policy program."""
    return APPROVAL_POLICY_RESOURCE_URI


def approval_policy_proposals_index_uri() -> str:
    """Root URI for policy proposals (index)."""
    return APPROVAL_POLICY_PROPOSALS_INDEX_URI


def approval_policy_proposal_item_uri(proposal_id: str) -> str:
    """URI for a specific policy proposal item under the proposals index."""
    return f"{APPROVAL_POLICY_PROPOSALS_INDEX_URI}/{proposal_id}"


# ---- Compositor meta ----


def compositor_meta_state_uri(server: str) -> str:
    return COMPOSITOR_META_STATE_URI_FMT.format(server=server)


def compositor_meta_state_prefix() -> str:
    # Build a safe prefix by formatting with an empty server name
    return COMPOSITOR_META_STATE_URI_FMT.format(server="")


def compositor_meta_instructions_uri(server: str) -> str:
    return COMPOSITOR_META_INSTRUCTIONS_URI_FMT.format(server=server)


def compositor_meta_capabilities_uri(server: str) -> str:
    return COMPOSITOR_META_CAPABILITIES_URI_FMT.format(server=server)


# Internal module — keep imports explicit at call sites

# ---- Backward-compat small helpers used by some modules ----


def compositor_state_uri_prefix() -> str:
    """Alias for the compositor meta state URI prefix."""
    return compositor_meta_state_prefix()


def parse_compositor_state_server(uri: str) -> str | None:
    """Extract the server name from a compositor meta state URI, if present."""
    prefix = compositor_meta_state_prefix()
    if uri.startswith(prefix):
        return uri[len(prefix) :]
    legacy = f"{COMPOSITOR_META_SERVER_NAME}/state/"
    if uri.startswith(f"resource://{legacy}"):
        return uri.split("/", 3)[-1]
    return None


# ---- Resources server synthetic URIs ----


def subscriptions_index_uri() -> str:
    return RESOURCES_SUBSCRIPTIONS_INDEX_URI
