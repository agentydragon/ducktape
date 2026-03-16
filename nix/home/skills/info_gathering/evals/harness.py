"""Shared eval harness for info-gathering skill.

Agent (uses skill) vs Simulator (holds ground truth). Provides:
- LLMClient wrapping LiteLLM (call, resolve_tool_calls)
- Conversation eval runner (run_conversation_eval)
- CLI utilities (add_common_args, load_skill, etc.)
- Pydantic models for results and logging

Supports any LiteLLM-compatible model string, e.g.:
  anthropic/claude-haiku-4-5-20251001
  openai/gpt-oss:20b  (with --base-url for custom endpoints)
"""

import argparse
import contextlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import litellm
from litellm.types.utils import Usage
from pydantic import BaseModel

from agent_core.direct_provider import DirectToolProvider
from agent_core.tool_provider import ToolProvider
from nix.home.skills.info_gathering.evals.litellm_tool_provider import (
    ToolParam,
    tool_params_from_provider,
    tool_result_content,
)
from openai_utils.json_schema import openai_json_schema
from util.bazel.runfiles import get_required_path

logger = logging.getLogger(__name__)

# Suppress litellm's verbose logging by default
litellm.suppress_debug_info = True


# === Pydantic models ==========================================================


class Judged(BaseModel):
    """Result from simulator judgment or Python-side scoring."""

    outcome: Literal["correct", "incorrect", "partial", "timeout"]
    score: float = 0
    summary: str = ""


class EndGameInput(BaseModel):
    """Input schema for the end_game tool (simulator terminates the game)."""

    outcome: Literal["correct", "incorrect", "partial"]
    score: float
    summary: str


class LogEntry(BaseModel):
    timestamp: str
    eval_name: str
    player: Literal["agent", "simulator"]
    turn: int
    model: str
    content: str
    reasoning_content: str | None = None
    tool_calls: list[dict[str, Any]] = []
    stop_reason: str
    usage: dict[str, Any]


class TokenTracker(BaseModel):
    model: str
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    PRICING: dict[str, dict[str, float]] = {
        "anthropic/claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0},
        "anthropic/claude-sonnet-4-6-20250514": {"input": 3.0, "output": 15.0},
        "anthropic/claude-opus-4-6-20250514": {"input": 15.0, "output": 75.0},
    }

    def add(self, usage: Usage) -> None:
        self.input_tokens += usage.prompt_tokens or 0
        self.output_tokens += usage.completion_tokens or 0
        self.api_calls += 1

    @property
    def cost_usd(self) -> float:
        p = self.PRICING.get(self.model, {"input": 1.0, "output": 5.0})
        return (self.input_tokens * p["input"] + self.output_tokens * p["output"]) / 1_000_000


class RunSummary(BaseModel):
    eval_name: str
    model: str
    turns: int
    result: BaseModel
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    api_cost_usd: float = 0


# === Tool helpers ==============================================================


def tool_def(name: str, description: str, input_model: type[BaseModel]) -> ToolParam:
    """Build an OpenAI-format tool definition from a Pydantic model."""
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": openai_json_schema(input_model)},
    }


# === LLM client ===============================================================


def extract_text(response: Any) -> str:
    """Extract text content from a LiteLLM ModelResponse."""
    msg = response.choices[0].message
    return msg.content or ""


def extract_tool_calls(response: Any) -> list[Any]:
    """Extract tool calls from a LiteLLM ModelResponse."""
    msg = response.choices[0].message
    return msg.tool_calls or []


class LLMClient:
    """Wraps LiteLLM completion with shared config (model, base_url, api_key, thinking)."""

    def __init__(
        self, *, model: str, thinking_budget: int | None = None, base_url: str | None = None, api_key: str | None = None
    ) -> None:
        self.model = model
        self.thinking_budget = thinking_budget
        self.base_url = base_url
        self.api_key = api_key

    async def call(
        self,
        *,
        messages: list[dict[str, Any]],
        system: str,
        tools: list[ToolParam] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        max_tokens: int = 4096,
    ) -> Any:
        """Call the LLM via LiteLLM."""
        full_messages: list[dict[str, Any]] = [{"role": "system", "content": system}, *messages]

        # Reasoning models (e.g. gpt-oss) need max_completion_tokens instead of max_tokens,
        # otherwise reasoning consumes the entire token budget leaving empty content.
        if self.model.startswith("anthropic/"):
            token_kwarg = {"max_tokens": max_tokens}
        else:
            token_kwarg = {"max_completion_tokens": max_tokens}

        thinking = None
        if self.thinking_budget and self.model.startswith("anthropic/"):
            thinking = {"type": "enabled", "budget_tokens": self.thinking_budget}

        return await litellm.acompletion(
            model=self.model,
            messages=full_messages,
            tools=tools,
            tool_choice=tool_choice,
            api_base=self.base_url,
            api_key=self.api_key,
            thinking=thinking,
            **token_kwarg,
        )

    async def resolve_tool_calls(
        self,
        *,
        response: Any,
        messages: list[dict[str, Any]],
        system: str,
        provider: ToolProvider,
        max_tokens: int = 4096,
    ) -> tuple[Any, list[dict[str, Any]], list[Usage]]:
        """Keep calling API until no more tool_use stops. Returns final response."""
        tools = await tool_params_from_provider(provider)
        usages: list[Usage] = []
        messages = list(messages)

        while response.choices[0].finish_reason == "tool_use" or (
            response.choices[0].finish_reason == "stop" and extract_tool_calls(response)
        ):
            tcs = extract_tool_calls(response)
            if not tcs:
                break

            # Append assistant message with tool calls
            messages.append(_serialize_message(response.choices[0].message))

            # Build tool result messages
            for tc in tcs:
                args = json.loads(tc.function.arguments)
                result = await provider.call_tool(tc.function.name, args)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": tool_result_content(result)})

            response = await self.call(messages=messages, system=system, tools=tools, max_tokens=max_tokens)
            usages.append(response.usage)

        return response, messages, usages


# === Logging/saving helpers ===================================================


def _serialize_message(msg: Any) -> dict[str, Any]:
    """Serialize a LiteLLM message to a typed dict for conversation history."""
    result: dict[str, Any] = {"role": "assistant", "content": msg.content}
    if msg.tool_calls:
        result["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments},
            }
            for tc in msg.tool_calls
        ]
    return result


def log_response(
    log_entries: list[LogEntry],
    *,
    name: str,
    player: Literal["agent", "simulator"],
    turn: int,
    model: str,
    response: Any,
) -> None:
    msg = response.choices[0].message
    tool_calls_data = []
    if msg.tool_calls:
        tool_calls_data = [
            {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments} for tc in msg.tool_calls
        ]

    # LiteLLM's Message type declares reasoning_content as Optional[str],
    # but deletes the attribute when None for OpenAI spec compatibility.
    reasoning: str | None = None
    with contextlib.suppress(AttributeError):
        reasoning = msg.reasoning_content

    log_entries.append(
        LogEntry(
            timestamp=datetime.now(UTC).isoformat(),
            eval_name=name,
            player=player,
            turn=turn,
            model=model,
            content=msg.content or "",
            reasoning_content=reasoning,
            tool_calls=tool_calls_data,
            stop_reason=response.choices[0].finish_reason or "",
            usage=response.usage.model_dump(),
        )
    )


def save_results(*, name: str, log_entries: list[LogEntry], summary: RunSummary, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    prefix = output_dir / f"{name}_{ts}"

    calls_path = Path(f"{prefix}_calls.jsonl")
    calls_path.write_text("".join(entry.model_dump_json() + "\n" for entry in log_entries))

    summary_path = Path(f"{prefix}_summary.json")
    summary_path.write_text(summary.model_dump_json(indent=2))

    logger.info("Saved: %s_*", prefix)


# === CLI helpers ==============================================================

DEFAULT_MODEL = "anthropic/claude-haiku-4-5-20251001"
DEFAULT_THINKING = 5000

_SKILL_RLOCATION = "_main/nix/home/skills/info_gathering/SKILL.md"


def load_skill() -> str:
    return get_required_path(_SKILL_RLOCATION).read_text()


def build_agent_system(skill_text: str, extra_system: str = "") -> str:
    parts = ["Follow this information-gathering skill throughout.\n\n<skill>\n" + skill_text + "\n</skill>"]
    if extra_system:
        parts.append("\n---\n\n" + extra_system)
    return "\n".join(parts)


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="LiteLLM model string, e.g. anthropic/claude-haiku-4-5-20251001 or openai/gpt-oss:20b",
    )
    parser.add_argument(
        "--thinking-budget", type=int, default=DEFAULT_THINKING, help="0 to disable (only for anthropic/ models)"
    )
    parser.add_argument("--output-dir", default=None)
    parser.add_argument(
        "--base-url", default=None, help="Custom API base URL (e.g. https://ollama-api.allegedly.works)"
    )
    parser.add_argument("--api-key", default=None, help="API key (reads from provider env var by default)")


def thinking_from_args(args: argparse.Namespace) -> int | None:
    return args.thinking_budget if args.thinking_budget > 0 else None


def output_dir_from_args(args: argparse.Namespace) -> Path:
    d = Path(args.output_dir) if args.output_dir else Path("eval_results") / datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    return d


def client_from_args(args: argparse.Namespace) -> LLMClient:
    return LLMClient(
        model=args.model, thinking_budget=thinking_from_args(args), base_url=args.base_url, api_key=args.api_key
    )


# === Conversation eval runner =================================================


async def run_conversation_eval(
    *,
    name: str,
    client: LLMClient,
    agent_system: str,
    first_user_message: str,
    sim_system: str,
    turn_limit: int = 20,
    output_dir: Path,
) -> RunSummary:
    """Run a conversation eval: agent and simulator exchange text.

    Simulator may call end_game to finish. Agent has no tools in this pattern.
    """
    tracker = TokenTracker(model=client.model)
    log_entries: list[LogEntry] = []
    result: Judged | None = None

    agent_messages: list[dict[str, Any]] = [{"role": "user", "content": first_user_message}]
    sim_messages: list[dict[str, Any]] = []

    sim_provider = DirectToolProvider()

    @sim_provider.tool
    def end_game(args: EndGameInput) -> None:
        """End the game. Call when the agent states a final answer."""
        nonlocal result
        result = Judged(outcome=args.outcome, score=args.score, summary=args.summary)

    sim_tool_params = await tool_params_from_provider(sim_provider)

    for turn in range(1, turn_limit + 1):
        logger.info("Turn %d...", turn)

        # Agent turn (no tools)
        agent_resp = await client.call(messages=agent_messages, system=agent_system)
        tracker.add(agent_resp.usage)
        log_response(log_entries, name=name, player="agent", turn=turn, model=client.model, response=agent_resp)

        agent_msg = _serialize_message(agent_resp.choices[0].message)
        agent_messages.append(agent_msg)

        agent_text = (agent_msg["content"] or "").strip()
        if not agent_text:
            continue

        # Simulator turn (with tools)
        sim_messages.append({"role": "user", "content": agent_text})
        sim_resp = await client.call(messages=sim_messages, system=sim_system, tools=sim_tool_params)
        tracker.add(sim_resp.usage)
        log_response(log_entries, name=name, player="simulator", turn=turn, model=client.model, response=sim_resp)

        if extract_tool_calls(sim_resp):
            sim_resp, sim_messages, usages = await client.resolve_tool_calls(
                response=sim_resp, messages=sim_messages, system=sim_system, provider=sim_provider
            )
            for u in usages:
                tracker.add(u)
            log_response(log_entries, name=name, player="simulator", turn=turn, model=client.model, response=sim_resp)

        sim_msg = _serialize_message(sim_resp.choices[0].message)
        sim_messages.append(sim_msg)
        agent_messages.append({"role": "user", "content": (sim_msg["content"] or "").strip()})

        if result:
            break
    else:
        result = Judged(outcome="timeout", summary=f"Hit {turn_limit} turn limit")

    summary = RunSummary(
        eval_name=name,
        model=client.model,
        turns=turn,
        result=result,
        api_calls=tracker.api_calls,
        input_tokens=tracker.input_tokens,
        output_tokens=tracker.output_tokens,
        api_cost_usd=round(tracker.cost_usd, 4),
    )
    save_results(name=name, log_entries=log_entries, summary=summary, output_dir=output_dir)
    return summary
