from agent_server.approvals import WellKnownTools
from agent_server.policies.policy_types import ApprovalDecision, PolicyRequest, PolicyResponse
from mcp_infra.constants import UI_MOUNT_PREFIX
from mcp_infra.naming import build_mcp_function

CONST_X = 42


TEST_CASES = [
    (
        PolicyRequest(name=build_mcp_function(UI_MOUNT_PREFIX, WellKnownTools.SEND_MESSAGE), arguments="{}"),
        ApprovalDecision.ALLOW,
    )
]


def decide(req: PolicyRequest) -> PolicyResponse:
    if CONST_X == 42:
        return PolicyResponse(decision=ApprovalDecision.ALLOW, rationale="ok")
    return PolicyResponse(decision=ApprovalDecision.ASK, rationale="no")


if __name__ == "__main__":
    from agent_server.policies.scaffold import run_with_tests

    raise SystemExit(run_with_tests(decide, TEST_CASES))
