"""Packaged minimal policy program: allow UI messaging and resource reads."""

from __future__ import annotations

from agent_server.policies.policy_types import ApprovalDecision, PolicyRequest, PolicyResponse
from agent_server.policies.scaffold import run
from mcp_infra.constants import RESOURCES_MOUNT_PREFIX, UI_MOUNT_PREFIX
from mcp_infra.naming import build_mcp_function, server_matches

# NOTE: Standalone policy program - these constants are acceptable here as it runs
# in isolation without compositor access. Alternative would be env vars.
UI_SEND = build_mcp_function(UI_MOUNT_PREFIX, "send_message")
UI_END = build_mcp_function(UI_MOUNT_PREFIX, "end_turn")


def decide(req: PolicyRequest) -> PolicyResponse:
    if req.name in (UI_SEND, UI_END):
        return PolicyResponse(decision=ApprovalDecision.ALLOW, rationale="UI communication")
    if server_matches(req.name, server=RESOURCES_MOUNT_PREFIX):
        return PolicyResponse(decision=ApprovalDecision.ALLOW, rationale="resource operations allowed")
    return PolicyResponse(decision=ApprovalDecision.ASK, rationale="default: ask")


if __name__ == "__main__":
    raise SystemExit(run(decide))
