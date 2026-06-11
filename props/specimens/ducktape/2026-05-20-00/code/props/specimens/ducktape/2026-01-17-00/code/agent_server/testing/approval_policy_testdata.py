"""Approval policy test fixtures package.

Provides fetch_policy(name) to load policy source text from this package.
"""

from __future__ import annotations

from importlib.resources import files
from typing import cast

from agent_server.policies.policy_types import ApprovalDecision


def fetch_policy(name: str) -> str:
    """Return policy Python source from a file named "<name>.py" in this package."""
    return cast(
        str,
        files(__name__.replace(".approval_policy_testdata", ".testdata.approval_policy"))
        .joinpath(f"{name}.py")
        .read_text(encoding="utf-8"),
    )


def make_policy(
    *,
    decision_expr: str,
    server: str,
    tool: str,
    default: ApprovalDecision = ApprovalDecision.ASK,
    doc: str | None = None,
) -> str:
    """
    Build a minimal ApprovalPolicy source that returns `decision_expr` for a given
    server/tool, and `default` otherwise. UI send_message is always allowed via
    TEST_CASES to satisfy baseline constraints.

    decision_expr examples: 'ApprovalDecision.DENY_CONTINUE', 'ApprovalDecision.ASK'
    """
    if default not in {ApprovalDecision.ASK, ApprovalDecision.ALLOW}:
        raise ValueError(f"default must be ASK or ALLOW, got {default}")
    default_expr = "ApprovalDecision.ASK" if default == ApprovalDecision.ASK else "ApprovalDecision.ALLOW"
    doc = doc or f"policy for {server}.{tool} returns explicit decision; default {default.value}"
    header = (
        "from agent_server.policies.policy_types import PolicyRequest, PolicyResponse, ApprovalDecision\n"
        "from agent_server.approvals import WellKnownTools\n"
        "from agent_server.policies.scaffold import run_with_tests\n"
        "from mcp_infra.constants import UI_MOUNT_PREFIX\n"
        "from mcp_infra.naming import build_mcp_function, tool_matches\n\n"
    )
    body = f"""
# {doc}
TEST_CASES = [
    (PolicyRequest(name=build_mcp_function(UI_MOUNT_PREFIX, WellKnownTools.SEND_MESSAGE), arguments_json="{{}}"), ApprovalDecision.ALLOW),
]

def decide(req: PolicyRequest) -> PolicyResponse:
    # Always allow UI send_message to satisfy baseline TEST_CASES
    if tool_matches(req.name, server=UI_MOUNT_PREFIX, tool=WellKnownTools.SEND_MESSAGE):
        return PolicyResponse(decision=ApprovalDecision.ALLOW, rationale='ui allow')
    if tool_matches(req.name, server='{server}', tool='{tool}'):
        return PolicyResponse(decision={decision_expr}, rationale='explicit')
    return PolicyResponse(decision={default_expr}, rationale='default')

if __name__ == '__main__':
    raise SystemExit(run_with_tests(decide, TEST_CASES))
"""
    return header + body
