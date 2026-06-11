from mcp_infra.naming import build_mcp_function
from mcp_infra.prefix import MCPMountPrefix
from x.agent_server.policies.policy_types import ApprovalDecision, PolicyRequest, PolicyResponse

TEST_CASES = [
    # Expect DENY_ABORT for UI send_message, but decide() returns ALLOW → preflight fails
    (
        PolicyRequest(name=build_mcp_function(MCPMountPrefix("ui"), "send_message"), arguments_json="{}"),
        ApprovalDecision.DENY_ABORT,
    )
]


def decide(req: PolicyRequest) -> PolicyResponse:
    # Intentionally violate expected semantics (used in tests)
    return PolicyResponse(decision=ApprovalDecision.ALLOW, rationale="ok")


if __name__ == "__main__":
    from x.agent_server.policies.scaffold import run_with_tests

    raise SystemExit(run_with_tests(decide, TEST_CASES))
