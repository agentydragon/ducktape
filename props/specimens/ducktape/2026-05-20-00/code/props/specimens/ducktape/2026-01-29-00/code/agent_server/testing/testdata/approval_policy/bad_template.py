from agent_server.approvals import WellKnownTools
from agent_server.policies.policy_types import ApprovalDecision, PolicyRequest, PolicyResponse
from agent_server.policies.scaffold import run_with_tests
from mcp_infra.constants import SEATBELT_EXEC_MOUNT_PREFIX, UI_MOUNT_PREFIX
from mcp_infra.naming import build_mcp_function, tool_matches

TEST_CASES = [
    (
        PolicyRequest(name=build_mcp_function(UI_MOUNT_PREFIX, WellKnownTools.SEND_MESSAGE), arguments="{}"),
        ApprovalDecision.ASK,
    )
]


def decide(req: PolicyRequest) -> PolicyResponse:
    if tool_matches(req.name, server=SEATBELT_EXEC_MOUNT_PREFIX, tool=WellKnownTools.SANDBOX_EXEC):
        # Intentionally error to simulate failing seatbelt resolution → deny-abort upstream
        raise RuntimeError("seatbelt policy resolution failed")
    return PolicyResponse(decision=ApprovalDecision.ASK, rationale="default")


if __name__ == "__main__":
    raise SystemExit(run_with_tests(decide, TEST_CASES))
