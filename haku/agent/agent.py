"""Assemble Haku's Microsoft Agent Framework agent and run one scan pass.

Runtime C (see <../plans/runtime_options.md>): the agent loop runs here, in-process
and provider-agnostic. Model calls go through the in-cluster LiteLLM proxy
(OpenAI-compatible), so the provider (Anthropic / OpenAI / Z.AI-GLM) is a LiteLLM
config knob (`HAKU_MODEL`), not code. Tools are a `run_command` shell tool (the Pod
is the trust boundary — see <../PLAN.md>) plus remote MCP toolsets (Tana to start).
Behavior is the baked `haku/base/` manual + `haku/run.md`, read at runtime, so it
stays single-sourced in ducktape. Session history persists in Valkey/Redis
(`RedisHistoryProvider`) when `HAKU_REDIS_URL` is set, else in-memory;
`SummarizationStrategy` keeps the instruction prefix and, once history fills,
LLM-summarizes the oldest turns into a running summary rather than dropping them.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

import httpx
from agent_framework import (
    Agent,
    AgentSession,
    FunctionInvocationConfiguration,
    FunctionTool,
    InMemoryHistoryProvider,
    MCPStreamableHTTPTool,
    SummarizationStrategy,
)
from agent_framework.openai import OpenAIChatCompletionClient
from agent_framework_redis import RedisHistoryProvider

from haku.agent.config import Settings

# Tail tool output so a chatty command can't blow the context window.
_MAX_TOOL_OUTPUT = 20_000

WAKE = "Wake: execute one scan pass per the run procedure, then commit, push, and stop."


async def _run_command(command: str, *, cwd: Path) -> str:
    proc = await asyncio.create_subprocess_shell(
        command, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    return out.decode(errors="replace")[-_MAX_TOOL_OUTPUT:]


def _run_command_tool(settings: Settings) -> FunctionTool:
    async def run_command(command: str) -> str:
        return await _run_command(command, cwd=settings.state_dir)

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


def build_mcp_tools(settings: Settings) -> list[MCPStreamableHTTPTool]:
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


def build_history_provider(settings: Settings) -> InMemoryHistoryProvider | RedisHistoryProvider:
    """Durable session history in Valkey/Redis when HAKU_REDIS_URL is set, else in-memory.

    The same session id keys the same Redis list, so history survives pod restarts; git
    (haku-state) remains the durable memory regardless."""
    if settings.redis_url:
        return RedisHistoryProvider(redis_url=settings.redis_url, max_messages=settings.redis_max_messages)
    return InMemoryHistoryProvider()


async def aclose_history(history: InMemoryHistoryProvider | RedisHistoryProvider) -> None:
    """Release the Redis connection; in-memory history needs no teardown."""
    if isinstance(history, RedisHistoryProvider):
        await history.aclose()


def build_agent(
    settings: Settings, mcp_tools: list[MCPStreamableHTTPTool], history: InMemoryHistoryProvider | RedisHistoryProvider
) -> Agent:
    client = build_client(settings)
    tools: list[FunctionTool | MCPStreamableHTTPTool] = [_run_command_tool(settings), *mcp_tools]
    return Agent(
        client=client,
        name="haku",
        instructions=_instructions(settings),
        tools=tools,
        context_providers=[history],
        # Keep the (cached) instruction prefix, append turns, and once history grows past
        # target+threshold groups, LLM-summarize the oldest into a running summary and
        # continue — rather than hard-dropping old turns.
        compaction_strategy=SummarizationStrategy(
            client=client, target_count=settings.summarize_target_count, threshold=settings.summarize_threshold
        ),
    )


async def run_scan(settings: Settings, *, message: str = WAKE) -> str:
    """Run one scan pass, resuming the persisted thread for `settings.session_id`."""
    mcp_tools = build_mcp_tools(settings)
    history = build_history_provider(settings)
    try:
        async with contextlib.AsyncExitStack() as stack:
            for tool in mcp_tools:
                await stack.enter_async_context(tool)
            agent = build_agent(settings, mcp_tools, history)
            response = await agent.run(message, session=AgentSession(session_id=settings.session_id))
        return response.text or ""
    finally:
        await aclose_history(history)
