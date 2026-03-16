"""Twenty Questions eval variants.

Usage:
  bazel run //nix/home/skills/info_gathering/evals/twenty_questions:twenty_questions_bin -- --variant states
  bazel run //nix/home/skills/info_gathering/evals/twenty_questions:twenty_questions_bin -- --variant states --model openai/gpt-oss:20b --base-url https://ollama-api.allegedly.works --thinking-budget 0
"""

import argparse
import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from agent_core.tool_provider import ToolProvider
from nix.home.skills.info_gathering.evals.docker_scratch import load_scratch_image, scratch_container
from nix.home.skills.info_gathering.evals.harness import (
    LLMClient,
    LogEntry,
    RunSummary,
    TokenTracker,
    _serialize_message,
    add_common_args,
    build_agent_system,
    client_from_args,
    extract_tool_calls,
    load_skill,
    log_response,
    output_dir_from_args,
    save_results,
    tool_def,
)
from nix.home.skills.info_gathering.evals.litellm_tool_provider import ToolParam, tool_params_from_provider
from util.bazel.runfiles import get_required_path

logger = logging.getLogger(__name__)

_SIM_RLOCATION = "_main/nix/home/skills/info_gathering/evals/twenty_questions/sim.txt"

_SCRATCH_SYSTEM_NOTE = """\
You have access to an `exec` tool — a private Docker container for scratch computation. \
Use it freely: run code, track hypothesis spaces, write notes, organize your reasoning. \
Calling this tool does NOT use up one of your question turns."""


class Correct(BaseModel):
    turns: int


class Timeout(BaseModel):
    limit: int


class AnswerInput(BaseModel):
    response: Literal["yes", "no", "sort_of"]


class CorrectAnswerInput(BaseModel):
    """The player correctly guessed the secret."""


ANSWER_TOOL = tool_def("answer", "Answer the player's yes/no question.", AnswerInput)
CORRECT_ANSWER_TOOL = tool_def("correct_answer", "The player correctly guessed the secret.", CorrectAnswerInput)

SIM_TOOLS: list[ToolParam] = [ANSWER_TOOL, CORRECT_ANSWER_TOOL]


@dataclass
class Variant:
    domain_description: str
    secret: str
    turn_limit: int = 20


VARIANTS: dict[str, Variant] = {
    "states": Variant(domain_description="a US state", secret="New Mexico"),
    "wide": Variant(
        domain_description="a thing — could be anything: object, place, concept, activity, anything",
        secret="a sourdough starter",
        turn_limit=25,
    ),
}


def _parse_sim_action(response: Any) -> tuple[str, str | None] | None:
    """Parse the simulator's tool call into (tool_name, answer_value).

    Returns None if no valid tool call found.
    """
    tcs = extract_tool_calls(response)
    if len(tcs) != 1:
        return None
    tc = tcs[0]
    if tc.function.name == "correct_answer":
        return ("correct_answer", None)
    if tc.function.name == "answer":
        args = json.loads(tc.function.arguments)
        validated = AnswerInput.model_validate(args)
        return ("answer", validated.response)
    return None


class _TwentyQuestionsRunner:
    """Runs a single 20Q eval game, tracking conversation state."""

    def __init__(
        self, *, name: str, client: LLMClient, agent_system: str, sim_system: str, agent_tool_provider: ToolProvider
    ) -> None:
        self.name = name
        self.client = client
        self.agent_system = agent_system
        self.sim_system = sim_system
        self.agent_tool_provider = agent_tool_provider
        self.tracker = TokenTracker(model=client.model)
        self.log_entries: list[LogEntry] = []
        self.agent_messages: list[dict[str, Any]] = []
        self.sim_messages: list[dict[str, Any]] = []
        self._last_tc_id: str | None = None

    async def _run_turn(self, turn: int, agent_tool_params: list[ToolParam]) -> Correct | None:
        """Run one agent+sim exchange. Returns Correct if guessed, None to continue."""
        # Agent turn — with optional scratch tools
        agent_resp = await self.client.call(
            messages=self.agent_messages, system=self.agent_system, tools=agent_tool_params or None
        )
        self.tracker.add(agent_resp.usage)
        log_response(
            self.log_entries, name=self.name, player="agent", turn=turn, model=self.client.model, response=agent_resp
        )

        # Resolve any scratch tool calls — does not count as a new turn
        if extract_tool_calls(agent_resp):
            agent_resp, self.agent_messages, usages = await self.client.resolve_tool_calls(
                response=agent_resp,
                messages=self.agent_messages,
                system=self.agent_system,
                provider=self.agent_tool_provider,
            )
            for u in usages:
                self.tracker.add(u)
            log_response(
                self.log_entries,
                name=self.name,
                player="agent",
                turn=turn,
                model=self.client.model,
                response=agent_resp,
            )

        agent_msg = _serialize_message(agent_resp.choices[0].message)
        self.agent_messages.append(agent_msg)

        agent_text = (agent_msg["content"] or "").strip()
        if not agent_text:
            return None

        # Sim turn — provide tool result from previous call if needed
        if self._last_tc_id:
            self.sim_messages.append({"role": "tool", "tool_call_id": self._last_tc_id, "content": "ok"})
        self._last_tc_id = None

        self.sim_messages.append({"role": "user", "content": agent_text})

        sim_resp = await self.client.call(
            messages=self.sim_messages, system=self.sim_system, tools=SIM_TOOLS, tool_choice="required"
        )
        self.tracker.add(sim_resp.usage)
        log_response(
            self.log_entries, name=self.name, player="simulator", turn=turn, model=self.client.model, response=sim_resp
        )

        if not extract_tool_calls(sim_resp):
            raise RuntimeError(
                f"Turn {turn}: simulator made no tool call despite tool_choice=required "
                f"(finish_reason={sim_resp.choices[0].finish_reason!r}, "
                f"content={sim_resp.choices[0].message.content!r})"
            )

        sim_msg = _serialize_message(sim_resp.choices[0].message)
        self.sim_messages.append(sim_msg)

        action = _parse_sim_action(sim_resp)
        if action is None:
            raise RuntimeError(f"Turn {turn}: simulator called unexpected tool, could not parse action")

        tool_name, answer = action

        if tool_name == "correct_answer":
            return Correct(turns=turn)

        # answer action
        assert answer is not None
        tcs = extract_tool_calls(sim_resp)
        if tcs:
            self._last_tc_id = tcs[0].id
        self.agent_messages.append({"role": "user", "content": answer})
        return None

    async def run(self, *, first_user_message: str, turn_limit: int, output_dir: Path) -> RunSummary:
        """Run the full game loop and return summary."""
        self.agent_messages.append({"role": "user", "content": first_user_message})

        # Compute agent tool params once (avoids repeated list_tools() calls per turn)
        agent_tool_params: list[ToolParam] = await tool_params_from_provider(self.agent_tool_provider)

        result: Correct | Timeout | None = None
        turn = 0
        for turn in range(1, turn_limit + 1):
            logger.info("Turn %d...", turn)
            result = await self._run_turn(turn, agent_tool_params)
            if result:
                break
        else:
            result = Timeout(limit=turn_limit)

        if turn == 0:
            result = Timeout(limit=turn_limit)
            turn = 0

        summary = RunSummary(
            eval_name=self.name,
            model=self.client.model,
            turns=turn,
            result=result,
            api_calls=self.tracker.api_calls,
            input_tokens=self.tracker.input_tokens,
            output_tokens=self.tracker.output_tokens,
            api_cost_usd=round(self.tracker.cost_usd, 4),
        )
        save_results(name=self.name, log_entries=self.log_entries, summary=summary, output_dir=output_dir)
        return summary


async def run_twenty_questions(
    *,
    name: str,
    client: LLMClient,
    agent_system: str,
    first_user_message: str,
    sim_system: str,
    turn_limit: int = 20,
    output_dir: Path,
    agent_tool_provider: ToolProvider,
) -> RunSummary:
    """Run a 20 Questions eval.

    Agent asks questions (optionally with scratch tools). Simulator answers via tool calls.
    Game ends when sim calls correct_answer or turns run out.
    """
    runner = _TwentyQuestionsRunner(
        name=name,
        client=client,
        agent_system=agent_system,
        sim_system=sim_system,
        agent_tool_provider=agent_tool_provider,
    )
    return await runner.run(first_user_message=first_user_message, turn_limit=turn_limit, output_dir=output_dir)


async def _async_main(args: argparse.Namespace) -> None:
    v = VARIANTS[args.variant]
    name = f"20q_{args.variant}"

    skill_text = load_skill()
    agent_system = build_agent_system(skill_text, extra_system=_SCRATCH_SYSTEM_NOTE)
    client = client_from_args(args)
    output_dir = output_dir_from_args(args)

    sim_template = get_required_path(_SIM_RLOCATION).read_text()
    sim_system = sim_template.format(secret=v.secret, turn_limit=v.turn_limit)

    first_user_message = (
        f"Play 20 Questions. I'm thinking of {v.domain_description}. "
        f"You have {v.turn_limit} yes/no questions. "
        "When confident, state: 'My answer is: [X]'."
    )

    logger.info("=" * 60)
    logger.info("  %s  |  %s  |  thinking=%s", name, client.model, client.thinking_budget or "off")
    logger.info("=" * 60)

    image = load_scratch_image()
    async with scratch_container(image) as provider:
        summary = await run_twenty_questions(
            name=name,
            client=client,
            agent_system=agent_system,
            first_user_message=first_user_message,
            sim_system=sim_system,
            turn_limit=v.turn_limit,
            output_dir=output_dir,
            agent_tool_provider=provider,
        )
    logger.info("%s", summary)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    p = argparse.ArgumentParser(description="Twenty Questions eval")
    add_common_args(p)
    p.add_argument("--variant", choices=list(VARIANTS), required=True)
    args = p.parse_args()

    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
