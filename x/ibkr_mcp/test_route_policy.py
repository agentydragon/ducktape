"""The read-only guarantee — the most important test in this package.

If any of these fail, a trading-capable route could reach a tool. The build
also enforces `allowlist ⊆ IBKR's real spec`: `spec_fixup.fix_spec` raises at
genrule time if an allowlisted path is missing upstream, so this suite covers
the other direction — that nothing dangerous is on the allowlist and the
generated spec contains exactly the allowlist and no more.
"""

from __future__ import annotations

import json

import pytest_bazel

from util.bazel.runfiles import get_required_path
from x.ibkr_mcp.route_policy import READ_ONLY_OPERATIONS

EXPECTED_TOOL_NAMES = {
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

# Substrings / prefixes that mark a route as trading- or account-capable. None
# of these may ever appear on the allowlist: this server is market-data + session
# only.
_FORBIDDEN_PATH_SUBSTRINGS = ("order", "reply", "logout")
_FORBIDDEN_PATH_PREFIXES = ("/iserver/account", "/portfolio", "/pa/", "/ibcust", "/fyi")


def test_tool_names_are_the_expected_read_only_set() -> None:
    assert {spec.name for spec in READ_ONLY_OPERATIONS.values()} == EXPECTED_TOOL_NAMES


def test_tool_names_are_unique() -> None:
    names = [spec.name for spec in READ_ONLY_OPERATIONS.values()]
    assert len(names) == len(set(names))


def test_no_trading_or_account_routes_on_allowlist() -> None:
    for method, path in READ_ONLY_OPERATIONS:
        assert method != "DELETE", f"DELETE is never read-only: {path}"
        lowered = path.lower()
        assert not any(s in lowered for s in _FORBIDDEN_PATH_SUBSTRINGS), f"forbidden substring in {path}"
        assert not any(path.startswith(p) for p in _FORBIDDEN_PATH_PREFIXES), f"forbidden prefix in {path}"


def test_generated_spec_contains_exactly_the_allowlist() -> None:
    spec = json.loads(get_required_path("_main/x/ibkr_mcp/ibkr.openapi.fixed.json").read_text())
    generated = {(method.upper(), path) for path, ops in spec["paths"].items() for method in ops}
    assert generated == set(READ_ONLY_OPERATIONS)


if __name__ == "__main__":
    pytest_bazel.main()
