"""Core eval logic: run an agent against a Grocy MCP server and record the rollout."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from agent_framework import Agent, AgentSession, InMemoryHistoryProvider, MCPStreamableHTTPTool, Message
from agent_framework.anthropic import AnthropicClient
from agent_framework.openai import OpenAIChatCompletionClient

from util.bazel.runfiles import get_required_path
from util.net import pick_free_port
from x.grocy_mcp.config import ServerSettings
from x.grocy_mcp.eval.cases import EvalCase
from x.grocy_mcp.eval.prompts import POSTMORTEM_PROMPT, SYSTEM_PROMPT
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


@asynccontextmanager
async def _serve_mcp(grocy_base_url: str) -> AsyncGenerator[str]:
    """Serve the Grocy MCP server on a local port and yield its URL."""
    port = pick_free_port()
    http_client = httpx.AsyncClient(base_url=f"{grocy_base_url}/api", timeout=30.0)
    mcp = build_mcp(ServerSettings(grocy_url=grocy_base_url), client=http_client)
    app = mcp.http_app(path="/mcp")

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    try:
        for _ in range(50):
            await asyncio.sleep(0.1)
            if server.started:
                break
        else:
            raise TimeoutError("MCP server did not start within 5s")

        mcp_url = f"http://127.0.0.1:{port}/mcp"
        logger.info("MCP server ready at %s", mcp_url)
        yield mcp_url
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=5.0)
        except TimeoutError:
            serve_task.cancel()
            await asyncio.gather(serve_task, return_exceptions=True)
        await http_client.aclose()


def _write_messages_jsonl(messages: list[Message], path: Path) -> None:
    """Write one JSON line per Message using agent_framework's own serde."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for msg in messages:
            f.write(json.dumps(msg.to_dict(), ensure_ascii=False, default=str) + "\n")


async def _snapshot_final_state(grocy_base_url: str) -> dict[str, Any]:
    """Dump every REST-visible Grocy entity after the agent finishes.

    The closest thing to a database dump that doesn't require reaching
    into the container: every table Grocy exposes via `/objects/<entity>`
    plus the computed stock / volatile views. IDs resolve to names via
    the reference tables in the same snapshot.
    """
    # Every entity Grocy exposes — covers core tables, `_view` / `_resolved`
    # SQL views, append-only audit tables, and computed aggregates. Kept
    # hard-coded rather than derived from `ReadableEntityType` so the
    # dump is self-contained and doesn't shift when the MCP tool surface
    # narrows.
    entity_types = [
        # Core writeable tables
        "products",
        "product_barcodes",
        "product_groups",
        "locations",
        "shopping_locations",
        "shopping_lists",
        "shopping_list",  # items (distinct entity from shopping_lists)
        "quantity_units",
        "quantity_unit_conversions",
        "recipes",
        "recipes_pos",
        "recipes_nestings",
        "meal_plan",
        "meal_plan_sections",
        "tasks",
        "task_categories",
        "chores",
        "batteries",
        "equipment",
        "userfields",
        "userentities",
        "userobjects",
        "api_keys",
        # Views / audit / computed
        "stock_log",
        "stock_current_locations",
        "chores_log",
        "products_last_purchased",
        "products_average_price",
        "quantity_unit_conversions_resolved",
        "recipes_pos_resolved",
        "battery_charge_cycles",
        "product_barcodes_view",
        "permission_hierarchy",
    ]
    paths = [f"/objects/{e}" for e in entity_types] + ["/stock", "/stock/volatile", "/system/info"]

    snapshot: dict[str, Any] = {}
    async with httpx.AsyncClient(base_url=f"{grocy_base_url}/api", timeout=30.0) as client:
        responses = await asyncio.gather(*(client.get(p) for p in paths), return_exceptions=True)
    for p, r in zip(paths, responses, strict=True):
        key = p.lstrip("/").replace("/", "_")
        if isinstance(r, BaseException):
            snapshot[key] = {"error": f"{type(r).__name__}: {r}"}
            continue
        if r.status_code != 200:
            snapshot[key] = {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
            continue
        try:
            data = r.json()
        except ValueError as e:
            snapshot[key] = {"error": f"Non-JSON response: {e}; body[:200]={r.text[:200]!r}"}
            continue
        snapshot[key] = data
        logger.info("snapshot %s: %d rows", p, len(data) if isinstance(data, list) else 1)
    return snapshot


async def run_grocy_eval(
    *, case: EvalCase, api: str, model: str, grocy_base_url: str, output_dir: Path, base_url: str | None = None
) -> EvalResult:
    """Run one eval case: seed → task → postmortem → snapshot."""
    output_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = output_dir / "transcript.jsonl"
    summary_path = output_dir / "summary.json"
    final_state_path = output_dir / "final_state.json"

    if case.seed is not None:
        logger.info("Seeding case %r", case.id)
        async with httpx.AsyncClient(base_url=f"{grocy_base_url}/api", timeout=30.0) as seed_client:
            await case.seed(seed_client)

    model_client = _build_model_client(api=api, model=model, base_url=base_url)
    session = AgentSession()
    # Attach explicitly instead of relying on Agent's auto-inject: if any
    # option triggers service-side storage (e.g. `store=True` default on
    # future clients), the auto-inject silently skips and the transcript
    # comes out empty.
    history_provider = InMemoryHistoryProvider()

    # The MCP server publishes `x/grocy_mcp/server_instructions.md` via
    # `initialize.instructions`, but `agent_framework` currently discards the
    # InitializeResult (see _mcp.py session.initialize()). Prepend the
    # markdown to the system prompt ourselves so the model sees it.
    server_instructions = get_required_path("_main/x/grocy_mcp/server_instructions.md").read_text()
    combined_instructions = f"{SYSTEM_PROMPT}\n\n{server_instructions}"

    async with _serve_mcp(grocy_base_url) as mcp_url:
        mcp_tool = MCPStreamableHTTPTool(name="grocy", url=mcp_url)
        async with mcp_tool:
            agent = Agent(
                client=model_client,
                name="grocy-eval",
                instructions=combined_instructions,
                tools=[mcp_tool],
                context_providers=[history_provider],
            )

            logger.info("Phase 1: Task (case=%s, model=%s, api=%s)", case.id, model, api)
            task_response = await agent.run(case.task_prompt, session=session)
            logger.info("Task complete: %s", (task_response.text or "")[:200])

            logger.info("Phase 2: Postmortem")
            postmortem_response = await agent.run(POSTMORTEM_PROMPT, session=session)
            postmortem_text = postmortem_response.text or ""
            logger.info("Postmortem: %s", postmortem_text[:500])

    final_state = await _snapshot_final_state(grocy_base_url)
    final_state_path.write_text(json.dumps(final_state, indent=2, ensure_ascii=False, default=str))

    # History providers store their messages under `session.state[source_id]`,
    # not `session.state` itself — see agent_framework._agents._run_after_hooks
    # which calls `state=provider_session.state.setdefault(provider.source_id, {})`.
    provider_state = session.state.get(history_provider.source_id, {})
    all_messages: list[Message] = list(provider_state.get("messages", []))
    _write_messages_jsonl(all_messages, transcript_path)
    logger.info("Transcript: %d messages written to %s", len(all_messages), transcript_path)

    result = EvalResult(
        case_id=case.id, success_criteria=case.success_criteria, model=model, api=api, postmortem_text=postmortem_text
    )
    summary_path.write_text(result.model_dump_json(indent=2))
    logger.info("Summary written to %s", summary_path)

    return result
