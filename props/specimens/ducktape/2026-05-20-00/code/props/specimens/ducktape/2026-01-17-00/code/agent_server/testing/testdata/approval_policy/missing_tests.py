from agent_server.policies.policy_types import ApprovalDecision, PolicyRequest, PolicyResponse


def decide(req: PolicyRequest) -> PolicyResponse:
    return PolicyResponse(decision=ApprovalDecision.ALLOW, rationale="ok")


if __name__ == "__main__":
    from agent_server.policies.scaffold import run

    raise SystemExit(run(decide))
