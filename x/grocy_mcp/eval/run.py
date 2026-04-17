"""Core eval logic: run an agent against a Grocy MCP server and record the rollout."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from agent_framework import Agent, AgentSession, MCPStreamableHTTPTool, Message
from agent_framework.anthropic import AnthropicClient
from agent_framework.openai import OpenAIChatCompletionClient

from x.grocy_mcp.config import ServerSettings
from x.grocy_mcp.eval.prompts import POSTMORTEM_PROMPT, SYSTEM_PROMPT, TASK_PROMPT
from x.grocy_mcp.eval.result_types import EvalResult
from x.grocy_mcp.server import build_mcp

logger = logging.getLogger(__name__)

DEFAULT_MODELS: dict[str, str] = {"openai": "gpt-4o-mini", "anthropic": "claude-haiku-4-5-20251001"}


def _build_model_client(*, api: str, model: str, base_url: str | None = None) -> Any:
    if api == "openai":
        kwargs: dict[str, Any] = {"model": model}
        if base_url:
            kwargs["base_url"] = base_url
        return OpenAIChatCompletionClient(**kwargs)
    if api == "anthropic":
        return AnthropicClient(model=model)
    raise ValueError(f"Unsupported API: {api!r}")


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


@asynccontextmanager
async def _serve_mcp(grocy_base_url: str) -> AsyncGenerator[str]:
    """Serve the Grocy MCP server on a local port and yield its URL."""
    port = _find_free_port()
    http_client = httpx.AsyncClient(base_url=f"{grocy_base_url}/api", timeout=30.0)
    mcp = build_mcp(ServerSettings(grocy_url=grocy_base_url), client=http_client)
    app = mcp.http_app(path="/mcp")

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    serve_task = asyncio.create_task(server.serve())
    for _ in range(50):
        await asyncio.sleep(0.1)
        if server.started:
            break
    else:
        raise TimeoutError("MCP server did not start within 5s")

    mcp_url = f"http://127.0.0.1:{port}/mcp"
    logger.info("MCP server ready at %s", mcp_url)
    try:
        yield mcp_url
    finally:
        server.should_exit = True
        await serve_task
        await http_client.aclose()


def _write_messages_jsonl(messages: list[Message], path: Path) -> None:
    """Write one JSON line per Message using agent_framework's own serde."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg.to_dict(), ensure_ascii=False, default=str) + "\n")


async def run_grocy_eval(
    *, api: str, model: str, grocy_base_url: str, output_dir: Path, base_url: str | None = None
) -> EvalResult:
    """Run the Grocy MCP eval: task execution + postmortem."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    transcript_path = output_dir / f"grocy_eval_{ts}_transcript.jsonl"
    summary_path = output_dir / f"grocy_eval_{ts}_summary.json"

    model_client = _build_model_client(api=api, model=model, base_url=base_url)
    session = AgentSession()

    async with _serve_mcp(grocy_base_url) as mcp_url:
        mcp_tool = MCPStreamableHTTPTool(name="grocy", url=mcp_url)
        async with mcp_tool:
            agent = Agent(client=model_client, name="grocy-eval", instructions=SYSTEM_PROMPT, tools=[mcp_tool])

            logger.info("Phase 1: Task execution (model=%s, api=%s)", model, api)
            task_response = await agent.run(TASK_PROMPT, session=session)
            logger.info("Task complete: %s", (task_response.text or "")[:200])

            logger.info("Phase 2: Postmortem")
            postmortem_response = await agent.run(POSTMORTEM_PROMPT, session=session)
            postmortem_text = postmortem_response.text or ""
            logger.info("Postmortem: %s", postmortem_text[:500])

    all_messages: list[Message] = list(session.state.get("messages", []))
    _write_messages_jsonl(all_messages, transcript_path)
    logger.info("Transcript: %d messages written to %s", len(all_messages), transcript_path)

    result = EvalResult(model=model, api=api, postmortem_text=postmortem_text, transcript_path=transcript_path)
    summary_path.write_text(result.model_dump_json(indent=2))
    logger.info("Summary written to %s", summary_path)

    return result
