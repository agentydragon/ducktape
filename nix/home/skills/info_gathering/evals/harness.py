"""Shared eval harness for info-gathering skill.

Agent (uses skill) vs Simulator (holds ground truth). Provides:
- API helpers (call_api, resolve_tool_calls)
- Conversation eval runner (run_conversation_eval)
- CLI utilities (add_common_args, load_skill, etc.)
- Pydantic models for results and logging
"""

import argparse
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import anthropic
import anthropic.types
from pydantic import BaseModel

from util.bazel.runfiles import get_required_path

logger = logging.getLogger(__name__)

ToolHandler = Callable[[str, dict[str, Any]], dict[str, Any]]


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


class Recommendation(BaseModel):
    title: str
    stars: int
    turn: int


class LogEntry(BaseModel):
    timestamp: str
    eval_name: str
    player: Literal["agent", "simulator"]
    turn: int
    model: str
    content: list[dict[str, Any]]
    stop_reason: str
    usage: dict[str, Any]


class TokenTracker(BaseModel):
    model: str
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    PRICING: dict[str, dict[str, float]] = {
        "claude-haiku-4-5-20251001": {"input": 1.0, "output": 5.0},
        "claude-sonnet-4-20250514": {"input": 3.0, "output": 15.0},
        "claude-opus-4-20250514": {"input": 15.0, "output": 75.0},
    }

    def add(self, usage: anthropic.types.Usage) -> None:
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
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
    recommendations: list[Recommendation] = []
    api_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    api_cost_usd: float = 0


# === Tool helpers ==============================================================


def tool_def(name: str, description: str, input_model: type[BaseModel]) -> anthropic.types.ToolParam:
    """Build an Anthropic API tool definition from a Pydantic model."""
    return anthropic.types.ToolParam(name=name, description=description, input_schema=input_model.model_json_schema())


END_GAME_TOOL = tool_def("end_game", "End the game. Call when the agent states a final answer.", EndGameInput)


# === API helpers ==============================================================


def _serialize_content(content: list[anthropic.types.ContentBlock]) -> list[dict[str, Any]]:
    """Serialize SDK content blocks to JSON-safe dicts for logging."""
    return [block.model_dump() for block in content]


def extract_text(response: anthropic.types.Message) -> str:
    """Extract text content from an Anthropic SDK response."""
    return "\n".join(block.text for block in response.content if isinstance(block, anthropic.types.TextBlock))


def extract_tool_calls(response: anthropic.types.Message) -> list[anthropic.types.ToolUseBlock]:
    return [block for block in response.content if isinstance(block, anthropic.types.ToolUseBlock)]


def call_api(
    *,
    client: anthropic.Anthropic,
    messages: list[anthropic.types.MessageParam],
    system: str,
    model: str,
    tools: list[anthropic.types.ToolParam] | None = None,
    tool_choice: anthropic.types.ToolChoiceParam | None = None,
    max_tokens: int = 4096,
    thinking_budget: int | None = None,
) -> anthropic.types.Message:
    """Call the Anthropic Messages API.

    Branches on optional params to satisfy the SDK's overloaded type stubs.
    """
    thinking: anthropic.types.ThinkingConfigEnabledParam | None = (
        anthropic.types.ThinkingConfigEnabledParam(type="enabled", budget_tokens=thinking_budget)
        if thinking_budget
        else None
    )
    if tools is not None and thinking is not None and tool_choice is not None:
        return client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            tools=tools,
            thinking=thinking,
            tool_choice=tool_choice,
        )
    if tools is not None and thinking is not None:
        return client.messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=messages, tools=tools, thinking=thinking
        )
    if tools is not None and tool_choice is not None:
        return client.messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=messages, tools=tools, tool_choice=tool_choice
        )
    if tools is not None:
        return client.messages.create(model=model, max_tokens=max_tokens, system=system, messages=messages, tools=tools)
    if thinking is not None:
        return client.messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=messages, thinking=thinking
        )
    return client.messages.create(model=model, max_tokens=max_tokens, system=system, messages=messages)


def resolve_tool_calls(
    *,
    client: anthropic.Anthropic,
    response: anthropic.types.Message,
    messages: list[anthropic.types.MessageParam],
    system: str,
    model: str,
    tools: list[anthropic.types.ToolParam],
    handler: ToolHandler,
    max_tokens: int = 4096,
    thinking_budget: int | None = None,
) -> tuple[anthropic.types.Message, list[anthropic.types.MessageParam], list[anthropic.types.Usage]]:
    """Keep calling API until no more tool_use stops. Returns final response."""
    usages: list[anthropic.types.Usage] = []
    messages = list(messages)

    while response.stop_reason == "tool_use":
        tool_results: list[anthropic.types.ToolResultBlockParam] = []
        for tc in extract_tool_calls(response):
            assert isinstance(tc.input, dict)
            result = handler(tc.name, tc.input)
            tool_results.append(
                anthropic.types.ToolResultBlockParam(
                    type="tool_result",
                    tool_use_id=tc.id,
                    content=json.dumps(result) if isinstance(result, dict) else str(result),
                )
            )
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})

        response = call_api(
            client=client,
            messages=messages,
            system=system,
            model=model,
            tools=tools,
            max_tokens=max_tokens,
            thinking_budget=thinking_budget,
        )
        usages.append(response.usage)

    return response, messages, usages


# === Logging/saving helpers ===================================================


def log_response(
    log_entries: list[LogEntry],
    *,
    name: str,
    player: Literal["agent", "simulator"],
    turn: int,
    model: str,
    response: anthropic.types.Message,
) -> None:
    log_entries.append(
        LogEntry(
            timestamp=datetime.now(UTC).isoformat(),
            eval_name=name,
            player=player,
            turn=turn,
            model=model,
            content=_serialize_content(response.content),
            stop_reason=response.stop_reason or "",
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

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
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
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--thinking-budget", type=int, default=DEFAULT_THINKING, help="0 to disable")
    parser.add_argument("--output-dir", default=None)


def make_client() -> anthropic.Anthropic:
    """Create Anthropic client. Reads ANTHROPIC_API_KEY from env."""
    return anthropic.Anthropic()


def thinking_from_args(args: argparse.Namespace) -> int | None:
    return args.thinking_budget if args.thinking_budget > 0 else None


def output_dir_from_args(args: argparse.Namespace) -> Path:
    d = Path(args.output_dir) if args.output_dir else Path("eval_results") / datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    return d


# === Conversation eval runner =================================================


def run_conversation_eval(
    *,
    name: str,
    client: anthropic.Anthropic,
    model: str,
    agent_system: str,
    first_user_message: str,
    sim_system: str,
    sim_tools: list[anthropic.types.ToolParam],
    turn_limit: int = 20,
    thinking_budget: int | None = None,
    output_dir: Path,
) -> RunSummary:
    """Run a conversation eval: agent and simulator exchange text.

    Simulator may call tools (end_game to finish, others passed through).
    Agent has no tools in this pattern.
    """
    tracker = TokenTracker(model=model)
    log_entries: list[LogEntry] = []
    result: Judged | None = None

    agent_messages: list[anthropic.types.MessageParam] = [{"role": "user", "content": first_user_message}]
    sim_messages: list[anthropic.types.MessageParam] = []

    def handle_sim_tool(tool_name: str, inp: dict[str, Any]) -> dict[str, Any]:
        nonlocal result
        if tool_name == "end_game":
            parsed = EndGameInput.model_validate(inp)
            result = Judged(outcome=parsed.outcome, score=parsed.score, summary=parsed.summary)
            return {"status": "game_ended"}
        return inp

    for turn in range(1, turn_limit + 1):
        logger.info("Turn %d...", turn)

        # Agent turn (no tools)
        agent_resp = call_api(
            client=client, messages=agent_messages, system=agent_system, model=model, thinking_budget=thinking_budget
        )
        tracker.add(agent_resp.usage)
        log_response(log_entries, name=name, player="agent", turn=turn, model=model, response=agent_resp)
        agent_messages.append({"role": "assistant", "content": agent_resp.content})

        agent_text = extract_text(agent_resp).strip()
        if not agent_text:
            continue

        # Simulator turn (with tools)
        sim_messages.append({"role": "user", "content": agent_text})
        sim_resp = call_api(
            client=client,
            messages=sim_messages,
            system=sim_system,
            model=model,
            tools=sim_tools,
            thinking_budget=thinking_budget,
        )
        tracker.add(sim_resp.usage)
        log_response(log_entries, name=name, player="simulator", turn=turn, model=model, response=sim_resp)

        if sim_resp.stop_reason == "tool_use":
            sim_resp, sim_messages, usages = resolve_tool_calls(
                client=client,
                response=sim_resp,
                messages=sim_messages,
                system=sim_system,
                model=model,
                tools=sim_tools,
                handler=handle_sim_tool,
                thinking_budget=thinking_budget,
            )
            for u in usages:
                tracker.add(u)
            log_response(log_entries, name=name, player="simulator", turn=turn, model=model, response=sim_resp)

        sim_messages.append({"role": "assistant", "content": sim_resp.content})
        sim_text = extract_text(sim_resp).strip()
        agent_messages.append({"role": "user", "content": sim_text})

        if result:
            break
    else:
        result = Judged(outcome="timeout", summary=f"Hit {turn_limit} turn limit")

    summary = RunSummary(
        eval_name=name,
        model=model,
        turns=turn,
        result=result,
        api_calls=tracker.api_calls,
        input_tokens=tracker.input_tokens,
        output_tokens=tracker.output_tokens,
        api_cost_usd=round(tracker.cost_usd, 4),
    )
    save_results(name=name, log_entries=log_entries, summary=summary, output_dir=output_dir)
    return summary
