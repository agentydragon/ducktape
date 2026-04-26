"""Standardized scratch-container shape for skill-eval rollouts.

`eval_sandbox` wraps `scratch_exec_mcp_tool` with the conventional
SKILL_FILES_PATH bind always in place: every rollout mounts its staged
skill (real or empty-skill placeholder) at the same in-container path,
so the agent sees a uniform sandbox shape across `--skill on/off` arms
and across evals. Callers add their own eval-specific binds (e.g. a
target binary, a writable workspace) via `extra_binds`.
"""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from agent_framework import MCPStdioTool

from mcp_infra.exec.docker.types import BindMount
from skills.eval_infra.af_scratch_mcp import scratch_exec_mcp_tool
from skills.eval_infra.skill_staging import SKILL_FILES_PATH, StagedSkill


@asynccontextmanager
async def eval_sandbox(
    *, skill: StagedSkill, extra_binds: Sequence[BindMount] = (), working_dir: Path = Path("/tmp")
) -> AsyncGenerator[MCPStdioTool]:
    """Yield an `MCPStdioTool` exposing `exec` against a scratch container with
    `skill` bind-mounted read-only at `SKILL_FILES_PATH` plus any `extra_binds`."""
    binds: list[BindMount] = [
        BindMount(host_path=skill.files_path.resolve(), container_path=SKILL_FILES_PATH, mode="ro"),
        *extra_binds,
    ]
    async with scratch_exec_mcp_tool(binds=binds, working_dir=working_dir) as tool:
        yield tool
