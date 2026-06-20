"""Assemble Haku's Microsoft Agent Framework agent and run one scan pass.

Runtime C (see <../plans/runtime_options.md>): the agent loop runs here, in-process
and provider-agnostic. Model calls go through the in-cluster LiteLLM proxy
(OpenAI-compatible), so the provider (Anthropic / OpenAI / Z.AI-GLM) is a LiteLLM
config knob (`HAKU_MODEL`), not code. Tools are a `run_command` shell tool (the Pod
is the trust boundary — see <../PLAN.md>) plus remote MCP toolsets (Tana to start).
Behavior is the baked `haku/base/` manual + `haku/run.md`, read at runtime, so it
stays single-sourced in ducktape. History is in-memory for now — cross-restart
persistence is a pending increment (Agent Framework ships no prebuilt Postgres
provider); `SlidingWindowStrategy` keeps the instruction prefix and bounds history.
"""

from __future__ import annotations

import asyncio
import contextlib

import httpx
from agent_framework import (
    Agent,
    AgentSession,
    FunctionInvocationConfiguration,
    FunctionTool,
    InMemoryHistoryProvider,
    MCPStreamableHTTPTool,
    SlidingWindowStrategy,
)
from agent_framework.openai import OpenAIChatCompletionClient

from haku.agent.config import Settings

# Tail tool output so a chatty command can't blow the context window.
_MAX_TOOL_OUTPUT = 20_000

WAKE = "Wake: execute one scan pass per the run procedure, then commit, push, and stop."


def _run_command_tool(settings: Settings) -> FunctionTool:
    async def run_command(command: str) -> str:
        proc = await asyncio.create_subprocess_shell(
            command, cwd=settings.state_dir, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await proc.communicate()
        return out.decode(errors="replace")[-_MAX_TOOL_OUTPUT:]

    return FunctionTool(
        name="run_command",
        description=(
            "Run a shell command (kubectl, psql, git, curl, fastmcp, …) from the haku-state "
            "checkout. The Pod is the trust boundary (mitmproxy egress + read-only creds + scoped "
            "RBAC), so everything reachable is read-only except haku-state. Returns combined "
            "stdout+stderr, tail-truncated."
        ),
        func=run_command,
    )


def _instructions(settings: Settings) -> str:
    return (
        "You are Haku, the operator's tireless background executive assistant. Your operating "
        f"manual is at {settings.base_dir}/base/instructions.md and your run procedure at "
        f"{settings.base_dir}/run.md; your haku-state checkout — your only memory and write "
        f"surface — is at {settings.state_dir}, with kubeconfig and git auth already in place. "
        "Read the manual and the run procedure with your tools, then execute the run procedure "
        "end to end: orient, process intake, scan your sources, write and curate items, append "
        "to the log, then commit and push. Each user message is a wake."
    )


def _mcp_tools(settings: Settings) -> list[MCPStreamableHTTPTool]:
    tools: list[MCPStreamableHTTPTool] = []
    if settings.tana_ro_token:
        # Bearer auth rides a pre-built http_client; the `headers=` kwarg is ignored in
        # later releases. Verify http_client against the pinned 1.0.0 before wiring Tana
        # for real — in-repo MCPStreamableHTTPTool usage so far is name+url only.
        tools.append(
            MCPStreamableHTTPTool(
                name="tana_ro",
                url="https://tana-mcp-ro.allegedly.works/mcp",
                http_client=httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {settings.tana_ro_token}"}, follow_redirects=True
                ),
            )
        )
    return tools


def build_client(settings: Settings) -> OpenAIChatCompletionClient:
    return OpenAIChatCompletionClient(
        model=settings.model,
        api_key=settings.litellm_api_key,
        base_url=settings.litellm_base_url,
        # Surface tool errors back to the model (it reads and retries) rather than aborting; raise
        # the inner roundtrip cap well past any realistic scan burst.
        function_invocation_configuration=FunctionInvocationConfiguration(
            include_detailed_errors=True, max_iterations=1000
        ),
    )


def build_agent(settings: Settings, mcp_tools: list[MCPStreamableHTTPTool]) -> Agent:
    tools: list[FunctionTool | MCPStreamableHTTPTool] = [_run_command_tool(settings), *mcp_tools]
    return Agent(
        client=build_client(settings),
        name="haku",
        instructions=_instructions(settings),
        tools=tools,
        # In-memory history for now (warm within a run). Cross-restart persistence
        # (Postgres/Redis) is the next increment — Agent Framework ships no prebuilt
        # Postgres provider, so the backend is a pending choice; git (haku-state) is the
        # durable memory regardless.
        context_providers=[InMemoryHistoryProvider()],
        compaction_strategy=SlidingWindowStrategy(keep_last_groups=settings.keep_last_groups),
    )


async def run_scan(settings: Settings, *, message: str = WAKE) -> str:
    """Run one scan pass, resuming the persisted thread for `settings.session_id`."""
    mcp_tools = _mcp_tools(settings)
    async with contextlib.AsyncExitStack() as stack:
        for tool in mcp_tools:
            await stack.enter_async_context(tool)
        agent = build_agent(settings, mcp_tools)
        response = await agent.run(message, session=AgentSession(session_id=settings.session_id))
    return response.text or ""
