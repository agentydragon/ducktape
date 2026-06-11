"""The shared `exec` tool, bound per-agent to a default working directory.

Every props agent exposes the same execve-based command runner; only the workspace
differs. This factory keeps the wrapper + model-facing description in one place instead
of re-declaring `async def exec(...)` in each agent. It uses the same schema-supplied
path as `direct_tool` (pass the JSON schema, validate into the model ourselves) so it
stays immune to MAF coercing JSON-native args — see props/agents/af/tools.py.
"""

from __future__ import annotations

from pathlib import Path

from agent_framework import FunctionTool

from mcp_infra.exec.models import BaseExecResult
from mcp_infra.exec.subprocess import DirectExecArgs, run_direct_exec

EXEC_DESCRIPTION = (
    "Run a program via execve (argv vector, NOT a shell). cmd[0] is the program (resolved on "
    "PATH); the rest are its literal arguments. No shell processing: pipes, redirects, globs, "
    "&&/;, quotes, and $VAR expansion are not interpreted. For shell features, invoke a shell "
    'explicitly: ["bash", "-lc", "..."].'
)


def make_exec_tool(default_cwd: Path) -> FunctionTool:
    """Build the `exec` FunctionTool bound to `default_cwd` (used when a call omits `cwd`)."""

    async def exec(**fields: object) -> BaseExecResult:
        return await run_direct_exec(DirectExecArgs.model_validate(fields), default_cwd=default_cwd)

    return FunctionTool(
        func=exec, name="exec", description=EXEC_DESCRIPTION, input_model=DirectExecArgs.model_json_schema()
    )
