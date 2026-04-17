"""Core eval logic: run an agent against a Grocy MCP server and record the rollout."""

from __future__ import annotations

import asyncio
import json
import logging
import socket
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from agent_framework import Agent, MCPStreamableHTTPTool, Message
from agent_framework.anthropic import AnthropicClient
from agent_framework.openai import OpenAIChatCompletionClient

from x.grocy_mcp.eval.prompts import POSTMORTEM_PROMPT, SYSTEM_PROMPT, TASK_PROMPT
from x.grocy_mcp.eval.result_types import EvalResult
from x.grocy_mcp.grocy_fixtures import make_settings
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
        return s.getsockname()[1]


@asynccontextmanager
async def _serve_mcp(grocy_base_url: str) -> AsyncGenerator[str]:
    """Serve the Grocy MCP server on a local port and yield its URL."""
    port = _find_free_port()
    http_client = httpx.AsyncClient(base_url=f"{grocy_base_url}/api", timeout=30.0)
    mcp = build_mcp(make_settings(grocy_base_url), client=http_client)
    app = mcp.http_app(path="/mcp")

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    serve_task = asyncio.create_task(server.serve())
    # Wait for server to start
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


def _serialize_messages(messages: list[Message], path: Path) -> None:
    """Write agent messages to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for msg in messages:
            record = {
                "ts": datetime.now(UTC).isoformat(),
                "role": msg.role,
                "content": [],
            }
            for content in msg.contents:
                record["content"].append(content.to_dict() if hasattr(content, "to_dict") else str(content))
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _count_tool_calls(messages: list[Message]) -> int:
    """Count function call content items in messages."""
    count = 0
    for msg in messages:
        for content in msg.contents:
            if content.type == "function_call":
                count += 1
    return count


async def run_grocy_eval(
    *,
    api: str,
    model: str,
    grocy_base_url: str,
    output_dir: Path,
    base_url: str | None = None,
) -> EvalResult:
    """Run the Grocy MCP eval: task execution + postmortem."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    transcript_path = output_dir / f"grocy_eval_{ts}_transcript.jsonl"
    summary_path = output_dir / f"grocy_eval_{ts}_summary.json"

    model_client = _build_model_client(api=api, model=model, base_url=base_url)

    async with _serve_mcp(grocy_base_url) as mcp_url:
        mcp_tool = MCPStreamableHTTPTool(name="grocy", url=mcp_url)
        async with mcp_tool:
            agent = Agent(
                client=model_client,
                name="grocy-eval",
                instructions=SYSTEM_PROMPT,
                tools=[mcp_tool],
            )

            # Phase 1: Task execution
            logger.info("Phase 1: Task execution (model=%s, api=%s)", model, api)
            task_response = await agent.run(TASK_PROMPT)
            task_messages = list(task_response.messages)
            task_text = task_response.text or ""
            task_turns = _count_tool_calls(task_messages)
            logger.info("Task complete: %d tool calls, response: %s", task_turns, task_text[:200])

            # Phase 2: Postmortem (continue same conversation)
            logger.info("Phase 2: Postmortem")
            history = task_messages + [Message("user", [POSTMORTEM_PROMPT])]
            postmortem_response = await agent.run(history)
            postmortem_messages = list(postmortem_response.messages)
            postmortem_text = postmortem_response.text or ""
            logger.info("Postmortem: %s", postmortem_text[:500])

    # Serialize full transcript
    all_messages = task_messages + [Message("user", [POSTMORTEM_PROMPT])] + postmortem_messages
    _serialize_messages(all_messages, transcript_path)
    logger.info("Transcript: %d messages written to %s", len(all_messages), transcript_path)

    result = EvalResult(
        model=model,
        api=api,
        task_turns=task_turns,
        postmortem_text=postmortem_text,
        transcript_path=transcript_path,
    )

    summary_path.write_text(result.model_dump_json(indent=2))
    logger.info("Summary written to %s", summary_path)

    if hasattr(model_client, "close"):
        await model_client.close()

    return result
