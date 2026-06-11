from __future__ import annotations

import sys
from collections.abc import Callable, Sequence

from agent_server.policies.policy_types import ApprovalDecision, PolicyRequest, PolicyResponse


def run(
    decide: Callable[[PolicyRequest], PolicyResponse],
    tests: Sequence[tuple[PolicyRequest, ApprovalDecision]] | None = None,
) -> int:
    """Run optional preflight tests, then wire stdin→decide→stdout once.

    - When `tests` are provided, evaluate each (PolicyRequest, expected ApprovalDecision)
      pair before reading stdin. On the first mismatch, raise RuntimeError. This keeps
      failures explicit and the policy container exits non-zero.
    - Reads a single PolicyRequest JSON from stdin, calls decide, and prints a
      PolicyResponse JSON to stdout.
    """
    if tests:
        for idx, (req, expected) in enumerate(tests):
            resp = decide(req)
            if not isinstance(resp, PolicyResponse):
                raise TypeError("decide() must return a PolicyResponse during preflight")
            if resp.decision is not expected:
                raise RuntimeError(f"preflight_failed: index={idx} expected={expected} got={resp.decision}")

    text = sys.stdin.read()
    req = PolicyRequest.model_validate_json(text)
    resp = decide(req)
    if not isinstance(resp, PolicyResponse):
        raise TypeError("decide() must return a PolicyResponse")
    print(resp.model_dump_json())
    return 0


def run_with_tests(
    decide: Callable[[PolicyRequest], PolicyResponse], tests: Sequence[tuple[PolicyRequest, ApprovalDecision]]
) -> int:
    """Backward-compatible alias: run with required preflight tests."""
    return run(decide, tests)


# Internal utility; import explicitly where needed (no barrels)
