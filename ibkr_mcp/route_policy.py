"""The read-only allowlist — the safety core of the IBKR MCP server.

IBKR's Client Portal Web API spec ships trading routes (order placement,
cancellation, replies) alongside the market-data routes. A single
authenticated gateway session can drive *either*. This module is the one
place that decides which operations ever become tools: only the read-only
market-data and session-lifecycle routes below are reflected; every
order/account-mutating route is absent by construction.

Two independent guards enforce this:

1. Build time — ``spec_fixup`` emits an OpenAPI document containing *only*
   these operations, so a forbidden route never even reaches FastMCP.
2. Runtime — ``server._customize_component`` raises if it is ever asked to
   surface an operation not listed here.

``test_route_policy`` asserts both that every entry exists in IBKR's real
spec and that no known trading route can slip in.
"""

from __future__ import annotations

from pydantic import BaseModel


class ToolSpec(BaseModel):
    """How one reflected IBKR operation is presented as a tool."""

    name: str
    extra_description: str = ""


# (HTTP method, IBKR spec path) → tool presentation.
#
# All of these are read-only: market-data reads, contract/security lookups,
# scanner reads, and session-lifecycle operations. `secdef/search`,
# `scanner/run`, `auth/status`, `reauthenticate`, and `tickle` are POSTs but
# carry no order/trade semantics — they read state or refresh the session.
READ_ONLY_OPERATIONS: dict[tuple[str, str], ToolSpec] = {
    ("GET", "/iserver/marketdata/snapshot"): ToolSpec(
        name="market_data_snapshot",
        extra_description=(
            "Top-of-book snapshot for one or more contracts. `conids` is a comma-separated list of "
            "contract IDs (resolve a symbol to a conid with `secdef_search` first). `fields` is a "
            "comma-separated list of numeric field codes (e.g. 31=last, 55=symbol, 84=bid, 86=ask, "
            "88=bid size, 85=ask size, 7295=open, 7296=close). On the free tier these return "
            "delayed values. IBKR requires a 'pre-flight' call: the first request for a fresh "
            "contract primes the stream and may return sparse data — call again for populated fields."
        ),
    ),
    ("GET", "/iserver/marketdata/history"): ToolSpec(
        name="market_data_history",
        extra_description=(
            "Historical bars for a contract (`conid`). `period` (e.g. 1d, 1w, 1m, 1y) sets the "
            "lookback and `bar` (e.g. 1min, 1h, 1d) the resolution. Delayed on the free tier."
        ),
    ),
    ("POST", "/iserver/secdef/search"): ToolSpec(
        name="secdef_search",
        extra_description="Resolve a symbol or company name to IBKR contract IDs (conids). Read-only lookup.",
    ),
    ("GET", "/iserver/secdef/info"): ToolSpec(
        name="secdef_info",
        extra_description="Full contract details for a conid (and optional derivative selectors). Read-only.",
    ),
    ("GET", "/iserver/secdef/strikes"): ToolSpec(
        name="secdef_strikes", extra_description="Option strike list for an underlying conid. Read-only."
    ),
    ("GET", "/iserver/contract/{conid}/info"): ToolSpec(
        name="contract_info", extra_description="Contract metadata for a single conid. Read-only."
    ),
    ("GET", "/iserver/scanner/params"): ToolSpec(
        name="scanner_params",
        extra_description="Available market-scanner parameters (scan codes, instrument types, filters). Read-only.",
    ),
    ("POST", "/iserver/scanner/run"): ToolSpec(
        name="scanner_run",
        extra_description="Run a market scanner and return matching contracts. Read-only screen; places no orders.",
    ),
    # ── Session lifecycle (thin wrappers, reflected from the same spec) ──
    ("POST", "/iserver/auth/status"): ToolSpec(
        name="session_status",
        extra_description=(
            "Report the gateway's authentication/session state. Use this before market-data calls; "
            "if it reports the session is not authenticated, call `request_reauth` — that triggers "
            "the IBKR Mobile push you approve on your phone (the weekly re-auth)."
        ),
    ),
    ("POST", "/iserver/reauthenticate"): ToolSpec(
        name="request_reauth",
        extra_description=(
            "Ask the gateway to re-establish the brokerage session. This fires the IBKR Mobile 2FA "
            "push to the account holder's phone; the tap itself happens out-of-band. Poll "
            "`session_status` afterwards until it reports authenticated. Places no orders."
        ),
    ),
    # NB: `/tickle` (session keepalive) is deliberately not a tool — IBeam maintains
    # the session for us; see TODO.md if that proves insufficient.
}


def tool_spec(method: str, path: str) -> ToolSpec:
    spec = READ_ONLY_OPERATIONS.get((method.upper(), path))
    if spec is None:
        raise KeyError(f"{method} {path} is not on the read-only allowlist")
    return spec
