from mcp_infra.constants import UI_MOUNT_PREFIX
from mcp_infra.naming import build_mcp_function
from x.agent_server.approvals import WellKnownTools
from x.agent_server.policies.policy_types import ApprovalDecision, PolicyRequest, PolicyResponse

TEST_CASES = [
    (
        PolicyRequest(name=build_mcp_function(UI_MOUNT_PREFIX, WellKnownTools.SEND_MESSAGE), arguments="{}"),
        ApprovalDecision.ALLOW,
    )
]


def decide(req: PolicyRequest) -> PolicyResponse:
    return PolicyResponse(decision=ApprovalDecision.ALLOW, rationale="ok")


if __name__ == "__main__":
    from x.agent_server.policies.scaffold import run_with_tests

    raise SystemExit(run_with_tests(decide, TEST_CASES))
