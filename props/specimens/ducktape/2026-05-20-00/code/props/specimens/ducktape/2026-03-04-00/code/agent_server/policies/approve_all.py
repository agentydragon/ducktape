"""Approve-all policy program (for noninteractive runs)."""

from __future__ import annotations

from agent_server.policies.policy_types import ApprovalDecision, PolicyRequest, PolicyResponse
from agent_server.policies.scaffold import run


def decide(_req: PolicyRequest) -> PolicyResponse:
    return PolicyResponse(decision=ApprovalDecision.ALLOW, rationale="allow all")


if __name__ == "__main__":
    raise SystemExit(run(decide))
