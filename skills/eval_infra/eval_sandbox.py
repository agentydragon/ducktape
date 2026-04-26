"""Standardized scratch-container exec sandbox for skill-eval rollouts.

Conventional in-container layout: every eval rollout's scratch container has

- ``/skill`` (`SKILL_PATH`)  — read-only bind of the staged skill (real or
  empty-skill placeholder; eval_sandbox always mounts this).
- ``/work``  (`WORK_PATH`)   — read-write workspace bind; the agent's default
  cwd; persisted on the host so the eval can harvest outputs after the run.
- ``/input/<name>`` (`INPUT_PATH / name`) — read-only mounts for
  eval-specific inputs (e.g. RE's target binary). Caller-driven via the
  ``inputs`` mapping.

`eval_sandbox` builds an `MCPStdioTool` that launches the
`mcp_infra.exec.docker.launcher` CLI as a subprocess (which boots a
`ContainerExecServer`) and speaks MCP over stdio; AF drives tool dispatch
natively — no FastMCP client, no hand-rolled `FunctionTool` bridge.

Usage:

    async with eval_sandbox(skill=staged, workspace=ws_dir) as exec_tool:
        agent = Agent(client=..., tools=[exec_tool, ...])
        await agent.run(...)
"""

import os
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from types import MappingProxyType

from agent_framework import MCPStdioTool

from mcp_infra.exec.docker.types import AlwaysSetTo, BindMount, ContainerExecServerConfig
from skills.eval_infra.skill_staging import StagedSkill
from util.bazel.runfiles import get_required_path

# In-container path conventions. Hardcoded — standardizing across rollouts is
# the whole point of `eval_infra`. See module docstring for the layout.
SKILL_PATH = Path("/skill")
WORK_PATH = Path("/work")
INPUT_PATH = Path("/input")

_LAUNCHER_RLOCATION = "_main/mcp_infra/exec/docker_launcher"
_NO_INPUTS: Mapping[str, Path] = MappingProxyType({})


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
    workspace: Path,
    inputs: Mapping[str, Path] = _NO_INPUTS,
    image: str = "python:3.13-slim",
    name: str = "exec",
) -> AsyncGenerator[MCPStdioTool]:
    """Yield an `MCPStdioTool` exposing `exec` against a scratch container.

    Args:
        skill: Staged skill to bind read-only at `SKILL_PATH`.
        workspace: Host directory bound read-write at `WORK_PATH`. Becomes the
            container cwd; the eval harvests anything the agent writes here.
        inputs: name → host path mapping; each entry is bound read-only at
            `INPUT_PATH / name`. The host path may be a file or a directory.
        image: Container image. Defaults to `python:3.13-slim`.
        name: MCPStdioTool tool name (defaults to ``"exec"``).

    The container has host networking, proxy env wired, and `cwd`/`user`/`env`
    fields hidden from the model.
    """
    binds: list[BindMount] = [
        BindMount(host_path=skill.files_path.resolve(), container_path=SKILL_PATH, mode="ro"),
        BindMount(host_path=workspace.resolve(), container_path=WORK_PATH, mode="rw"),
    ]
    for sub, host_path in inputs.items():
        binds.append(BindMount(host_path=host_path.resolve(), container_path=INPUT_PATH / sub, mode="ro"))

    config = ContainerExecServerConfig(
        image=image,
        working_dir=WORK_PATH,
        network_mode="host",
        environment=_proxy_env(),
        allow_user_field=False,
        allow_env_field=False,
        cwd_policy=AlwaysSetTo(value=WORK_PATH),
        binds=binds,
    )
    launcher = get_required_path(_LAUNCHER_RLOCATION)
    async with MCPStdioTool(name=name, command=str(launcher), args=["--config", config.model_dump_json()]) as tool:
        yield tool
