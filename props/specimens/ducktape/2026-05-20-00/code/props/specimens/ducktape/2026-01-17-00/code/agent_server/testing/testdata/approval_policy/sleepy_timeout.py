from time import sleep

from agent_server.policies.policy_types import ApprovalDecision, PolicyRequest, PolicyResponse
from mcp_infra.constants import UI_MOUNT_PREFIX
from mcp_infra.naming import build_mcp_function

TEST_CASES = [
    (PolicyRequest(name=build_mcp_function(UI_MOUNT_PREFIX, "send_message"), arguments="{}"), ApprovalDecision.ASK)
]


def decide(req: PolicyRequest) -> PolicyResponse:
    sleep(1.0)
    return PolicyResponse(decision=ApprovalDecision.ASK, rationale="done")


if __name__ == "__main__":
    from agent_server.policies.scaffold import run_with_tests

    raise SystemExit(run_with_tests(decide, TEST_CASES))
