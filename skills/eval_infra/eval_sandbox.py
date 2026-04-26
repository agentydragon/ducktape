"""Standardized scratch-container exec sandbox for skill-eval rollouts.

`eval_sandbox` builds an `MCPStdioTool` that launches the
`mcp_infra.exec.docker.launcher` CLI as a subprocess (which boots a
`ContainerExecServer`) and binds the staged skill at the conventional
`SKILL_FILES_PATH` plus any eval-specific extra binds. The launcher
speaks MCP over stdio; AF drives tool dispatch natively — no FastMCP
client, no hand-rolled `FunctionTool` bridge.

Every rollout mounts its skill (real or empty-skill placeholder) at the
same in-container path, so the agent sees a uniform sandbox shape
across `--skill on/off` arms and across evals. Eval-specific binds
(target binary, writable workspace, etc.) flow through `extra_binds`.

Usage:

    async with eval_sandbox(skill=staged) as exec_tool:
        agent = Agent(client=..., tools=[exec_tool, ...])
        await agent.run(...)
"""

import os
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from agent_framework import MCPStdioTool

from mcp_infra.exec.docker.types import AlwaysSetTo, BindMount, ContainerExecServerConfig
from skills.eval_infra.skill_staging import SKILL_FILES_PATH, StagedSkill
from util.bazel.runfiles import get_required_path

_LAUNCHER_RLOCATION = "_main/mcp_infra/exec/docker_launcher"


def _proxy_env() -> dict[str, str]:
    """Collect HTTP(S) proxy env vars for container networking."""
    env: dict[str, str] = {}
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy", "NO_PROXY", "no_proxy"):
        if val := os.environ.get(var):
            env[var] = val
    return env


@asynccontextmanager
async def eval_sandbox(
    *,
    skill: StagedSkill,
    extra_binds: Sequence[BindMount] = (),
    working_dir: Path = Path("/tmp"),
    image: str = "python:3.13-slim",
    name: str = "exec",
) -> AsyncGenerator[MCPStdioTool]:
    """Yield an `MCPStdioTool` exposing `exec` against a scratch container with
    `skill` bind-mounted read-only at `SKILL_FILES_PATH` plus any `extra_binds`.

    The container has host networking, proxy env wired, and `cwd`/`user`/`env`
    fields hidden from the model.
    """
    binds: list[BindMount] = [
        BindMount(host_path=skill.files_path.resolve(), container_path=SKILL_FILES_PATH, mode="ro"),
        *extra_binds,
    ]
    config = ContainerExecServerConfig(
        image=image,
        working_dir=working_dir,
        network_mode="host",
        environment=_proxy_env(),
        allow_user_field=False,
        allow_env_field=False,
        cwd_policy=AlwaysSetTo(value=working_dir),
        binds=binds,
    )
    launcher = get_required_path(_LAUNCHER_RLOCATION)
    async with MCPStdioTool(name=name, command=str(launcher), args=["--config", config.model_dump_json()]) as tool:
        yield tool
