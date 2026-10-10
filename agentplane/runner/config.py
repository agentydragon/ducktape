"""Runner-owned launch configuration. Credentials and endpoints never cross the protocol."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from agentplane.llm_ingress.models import ModelConfig
from agentplane.runner import protocol_pb2
from agentplane.runner.cgroups import AgentCgroups
from agentplane.runner.model_config import HttpModelConfigResolver, ModelConfigLookupError


@dataclass(frozen=True, slots=True)
class ClaudeLaunch:
    binary: Path
    # Anthropic Messages endpoint the harness talks to, without a path.
    base_url: str
    auth_token: str


@dataclass(frozen=True, slots=True)
class CodexLaunch:
    binary: Path
    # OpenAI Responses base URL including its `/v1`.
    base_url: str
    api_key: str


@dataclass(frozen=True, slots=True)
class DebugCheckpoint:
    """One test-only runner pause, surfaced through the ordinary session event stream."""

    name: str
    command_id: str


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    # Holds runner-owned logs and metadata.
    state_dir: Path
    # Base environment of every harness child, as --harness-env gave it; native credentials are
    # added per launch. This is not the runner process environment.
    harness_environment: Mapping[str, str] = field(default_factory=dict)
    # The deployed LLM ingress owns per-route client configuration. The runner resolves it
    # on session/model selection and retains the consumed budget in its session record.
    model_config_resolver: HttpModelConfigResolver = field(default_factory=HttpModelConfigResolver)
    claude: ClaudeLaunch | None = None
    codex: CodexLaunch | None = None
    # VM guests keep app work in a delegated cgroup and run it under a separate unprivileged UID.
    # Container deployments leave this unset and retain their existing process behavior.
    process_isolation: AgentCgroups | None = None
    # When set, new and retained session working directories must stay beneath this mount.
    workspace_root: Path | None = None
    # VM guests separate native harness histories from the runner's journal tree so project quota
    # can keep untrusted harness writes from consuming journal capacity. Containers keep the
    # existing per-session layout by leaving this unset.
    native_state_dir: Path | None = None
    # VM initialization/setup scripts work on the workspace disk, not the runner-owned state tree.
    initialization_cwd: Path | None = None
    # A hidden test-only process-integration seam. Deployments never set it. Once the runner
    # reaches this boundary it appends DebugCheckpoint and waits to be killed by the test; a
    # replacement runner sees that persisted event and proceeds normally.
    test_debug_checkpoint: DebugCheckpoint | None = None

    async def resolve_model_config(self, *, harness: int, model: str) -> ModelConfig | None:
        """Resolve a route using the selected harness's proxy endpoint and workload credential."""
        if harness == protocol_pb2.HARNESS_CLAUDE:
            if self.claude is None:
                raise ModelConfigLookupError("Claude is not configured on this runner")
            base_url, token = self.claude.base_url, self.claude.auth_token
        elif harness == protocol_pb2.HARNESS_CODEX:
            if self.codex is None:
                raise ModelConfigLookupError("Codex is not configured on this runner")
            base_url, token = self.codex.base_url, self.codex.api_key
        else:
            raise ModelConfigLookupError("unsupported harness for model-config lookup")
        return await self.model_config_resolver.resolve(base_url=base_url, token=token, model=model)
