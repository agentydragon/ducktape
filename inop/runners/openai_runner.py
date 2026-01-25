from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import aiodocker
from fastmcp.client import Client
from fastmcp.server import FastMCP

from agent_core.agent import Agent
from agent_core.handler import BaseHandler
from agent_core.loop_control import RequireAnyTool
from agent_core.mcp_provider import MCPToolProvider
from agent_core.transcript_handler import TranscriptHandler
from inop.engine.models import (
    FinalOutput,
    Rollout,
    RunnerEnvironment,
    TaskDefinition,
    TaskTypeConfig,
    TrajectoryItem,
    UserInput,
    WorkspaceEnvironment,
)
from inop.io.file_utils import collect_workspace_files
from inop.runners.base import AgentRunner
from mcp_infra.compositor.compositor import Compositor
from mcp_infra.constants import WORKING_DIR
from mcp_infra.display.event_renderer import DisplayEventsHandler
from mcp_infra.exec.bwrap import BwrapExecServer
from mcp_infra.exec.direct import DirectExecServer
from mcp_infra.exec.docker.container_session import BindMount, ContainerOptions
from mcp_infra.exec.docker.server import ContainerExecServer
from mcp_infra.mcp_types import NetworkMode
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.model import OpenAIModelProto, SystemMessage, UserMessage

"""OpenAI runner that delegates execution to the Agent agent."""


class OpenAIRunner(AgentRunner):
    """Runner that executes tasks via the Agent agent."""

    def __init__(
        self, runner_id: str, config: dict[str, Any], *, openai_model: OpenAIModelProto, docker_client: aiodocker.Docker
    ) -> None:
        super().__init__(runner_id, config)
        configured_model = config.get("model")
        self.model = (
            configured_model if isinstance(configured_model, str) else os.getenv("OPENAI_MODEL", "gpt-5.1-codex-mini")
        )
        self.reasoning_effort = config.get("reasoning_effort")
        self.workspace_path: Path | None = None
        self._exit_stack: AsyncExitStack | None = None
        self._agent: Agent | None = None
        # Note: Compositor + Client are managed per-setup; no manager retained
        self._openai_model = openai_model
        self._docker_client = docker_client
        # Optional: allow callers/tests to pass their own handlers
        self._handlers: list[BaseHandler] | None = (
            config.get("handlers") if isinstance(config.get("handlers"), list) else None
        )

    async def setup(self, task: TaskDefinition, task_type_config: dict[str, Any]) -> None:
        # Ensure proper typing for resolve_config: expects dict[str, TaskTypeConfig]
        typed_map: dict[str, TaskTypeConfig] = {task.type: TaskTypeConfig.model_validate(task_type_config)}
        setup, _ = task.resolve_config(typed_map)

        self.workspace_path = Path(tempfile.mkdtemp(prefix="minicodex_"))

        if setup and setup.git_clone:
            await self._clone_repository(setup.git_clone, str(self.workspace_path), is_docker=False)

        server_factories = self._build_mcp_server_factories(setup)

        self._exit_stack = AsyncExitStack()
        # Enter Compositor into exit stack for proper cleanup
        comp = await self._exit_stack.enter_async_context(Compositor())
        for name, factory in server_factories.items():
            await comp.mount_inproc(MCPMountPrefix(name), factory(None))
        # Per-run transcript directory
        run_dir = Path.cwd() / "logs" / "agent" / "openai_runner"
        run_dir = run_dir / f"run_{int(time.time())}_{os.getpid()}"
        run_dir.mkdir(parents=True, exist_ok=True)
        default_handlers: list = [DisplayEventsHandler(), TranscriptHandler(events_path=run_dir / "events.jsonl")]
        handlers: list[BaseHandler] = self._handlers or default_handlers
        mcp_client = await self._exit_stack.enter_async_context(Client(comp))
        agent = await Agent.create(
            tool_provider=MCPToolProvider(mcp_client),
            client=self._openai_model,
            reasoning_effort=self.reasoning_effort,
            handlers=handlers,
            tool_policy=RequireAnyTool(),
        )
        # Note: Agent doesn't own resources, no cleanup needed
        self._agent = agent

    def _build_mcp_server_factories(self, setup) -> dict[str, Callable[..., FastMCP]]:
        if not self.workspace_path:
            raise RuntimeError("Workspace not initialised")

        if setup and setup.docker:
            binds: list[BindMount] = [
                BindMount(host_path=self.workspace_path, container_path=Path("/workspace"), mode="rw")
            ]
            for host_path, spec in (setup.docker.volumes or {}).items():
                if isinstance(spec, dict):
                    binds.append(
                        BindMount(
                            host_path=Path(host_path), container_path=Path(spec["bind"]), mode=spec.get("mode", "rw")
                        )
                    )
            network_mode = NetworkMode.BRIDGE if setup.docker.network_enabled else NetworkMode.NONE

            def _factory(verifier) -> FastMCP:
                return ContainerExecServer(
                    self._docker_client,
                    ContainerOptions(
                        image=setup.docker.image,
                        working_dir=WORKING_DIR,
                        binds=binds,
                        network_mode=network_mode,
                        environment=setup.docker.env or {},
                        labels={"adgn.project": "inop", "adgn.role": "container"},
                    ),
                )

            return {"container": _factory}

        sandbox_enabled = True
        if setup and setup.sandbox:
            sandbox_enabled = setup.sandbox.enabled
        if os.getenv("DUCK_ALLOW_UNSANDBOXED") == "1":
            sandbox_enabled = False

        def _factory_local(verifier) -> FastMCP:
            # When sandbox is required, do not fall back to unsandboxed exec; crash instead.
            if sandbox_enabled:
                if os.name == "posix" and sys.platform == "linux":
                    return BwrapExecServer(default_cwd=self.workspace_path)
                raise RuntimeError("Sandbox (bubblewrap) required but not available on this platform")
            # Explicitly unsandboxed path allowed via config/env override
            return DirectExecServer(default_cwd=self.workspace_path)

        return {"local": _factory_local}

    async def run_task(self, task: TaskDefinition, agent_instructions: str) -> Rollout:
        if not self._agent:
            raise RuntimeError("Runner not initialised; call setup() first")

        self._agent.process_message(SystemMessage.text(agent_instructions))
        self._agent.process_message(UserMessage.text(task.prompt))

        start_time = time.perf_counter()
        result = await self._agent.run()

        trajectory: list[TrajectoryItem] = [UserInput(text=task.prompt)]
        # Agent intentionally does not expose its internal event sequence; callers requiring
        # fine-grained events must install a RecordingHandler when creating the agent.

        if result.text:
            trajectory.append(FinalOutput(text=result.text))

        assert self.workspace_path is not None, "Workspace path not initialised"
        files = collect_workspace_files(self.workspace_path)

        return Rollout(
            task_id=task.id,
            runner_id=self.runner_id,
            agent_id=f"{self.runner_id}_{uuid.uuid4().hex[:8]}",
            trajectory=trajectory,
            files=files,
            success=True,
            error_message=None,
            cost_usd=0.0,
            duration_seconds=time.perf_counter() - start_time,
            metadata={"workspace": str(self.workspace_path) if self.workspace_path else None},
        )

    async def cleanup(self) -> None:
        if self._exit_stack:
            await self._exit_stack.aclose()
            self._exit_stack = None
        if self.workspace_path and self.workspace_path.exists():
            shutil.rmtree(self.workspace_path)
            self.workspace_path = None

    def get_environment(self) -> RunnerEnvironment | None:
        if not self.workspace_path:
            return None
        return WorkspaceEnvironment(workspace_path=self.workspace_path)
