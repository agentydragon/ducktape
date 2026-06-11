from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Protocol

import aiodocker

from agent_server.policies.policy_types import PolicyRequest, PolicyResponse
from agent_server.policy_eval.runner import run_policy_source
from agent_server.runtime.images import resolve_runtime_image

logger = logging.getLogger(__name__)


class PolicyProvider(Protocol):
    """Protocol for policy source providers (e.g., PolicyEngine).

    TODO: This Protocol exists to break a circular dependency between
    policy_eval and mcp/approval_policy. The circle is:
    - policy_eval.container needs PolicyEngine type
    - approval_policy.engine imports policy_eval.container and policy_eval.runner
    Using a Protocol avoids the BUILD-level dependency.
    """

    def get_policy(self) -> str:
        """Return the current policy source code."""
        ...


@dataclass
class ContainerPolicyEvaluator:
    """Evaluate policy decisions inside a one-off Docker container (isolated).

    The active policy source is executed directly via `python -c <source>`; no
    per-agent volumes are required. The image must have the `adgn` package
    installed so the policy can import helpers. Network is disabled; no RW
    mounts; no container reuse.
    """

    agent_id: str
    docker_client: aiodocker.Docker
    engine: PolicyProvider
    image: str = field(default_factory=resolve_runtime_image)
    timeout_secs: float = field(default_factory=lambda: float(os.getenv("ADGN_POLICY_EVAL_TIMEOUT_SECS", "5")))

    async def decide(self, policy_input: PolicyRequest) -> PolicyResponse:
        """Evaluate using the current policy source via run_policy_source."""
        policy_src = self.engine.get_policy()
        return await run_policy_source(
            docker_client=self.docker_client,
            source=policy_src,
            input_payload=policy_input,
            image=self.image,
            timeout_secs=self.timeout_secs,
        )
