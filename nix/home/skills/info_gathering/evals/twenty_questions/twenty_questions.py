"""Twenty Questions eval variants.

Usage:
  bazel run //nix/home/skills/info_gathering/evals/twenty_questions:twenty_questions_bin -- --variant states
  bazel run //nix/home/skills/info_gathering/evals/twenty_questions:twenty_questions_bin -- --variant states --model openai/gpt-oss:20b --base-url https://ollama-api.allegedly.works --thinking-budget 0
"""

import argparse
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from nix.home.skills.info_gathering.evals.harness import (
    LLMClient,
    LogEntry,
    RunSummary,
    TokenTracker,
    ToolParam,
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
from util.bazel.runfiles import get_required_path

logger = logging.getLogger(__name__)

_SIM_RLOCATION = "_main/nix/home/skills/info_gathering/evals/twenty_questions/sim.txt"


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

    def __init__(self, *, name: str, client: LLMClient, agent_system: str, sim_system: str) -> None:
        self.name = name
        self.client = client
        self.agent_system = agent_system
        self.sim_system = sim_system
        self.tracker = TokenTracker(model=client.model)
        self.log_entries: list[LogEntry] = []
        self.agent_messages: list[dict[str, Any]] = []
        self.sim_messages: list[dict[str, Any]] = []
        self._last_tc_id: str | None = None

    def _run_turn(self, turn: int) -> Correct | None:
        """Run one agent+sim exchange. Returns Correct if guessed, None to continue."""
        # Agent turn (no tools)
        agent_resp = self.client.call(messages=self.agent_messages, system=self.agent_system)
        self.tracker.add(agent_resp.usage)
        log_response(
            self.log_entries, name=self.name, player="agent", turn=turn, model=self.client.model, response=agent_resp
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

        sim_resp = self.client.call(
            messages=self.sim_messages, system=self.sim_system, tools=SIM_TOOLS, tool_choice="auto"
        )
        self.tracker.add(sim_resp.usage)
        log_response(
            self.log_entries, name=self.name, player="simulator", turn=turn, model=self.client.model, response=sim_resp
        )

        sim_msg = _serialize_message(sim_resp.choices[0].message)
        self.sim_messages.append(sim_msg)

        action = _parse_sim_action(sim_resp)
        if action is None:
            logger.warning("Turn %d: could not parse simulator action", turn)
            self.agent_messages.append({"role": "user", "content": "(no response)"})
            return None

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

    def run(self, *, first_user_message: str, turn_limit: int, output_dir: Path) -> RunSummary:
        """Run the full game loop and return summary."""
        self.agent_messages.append({"role": "user", "content": first_user_message})

        result: Correct | Timeout | None = None
        turn = 0
        for turn in range(1, turn_limit + 1):
            logger.info("Turn %d...", turn)
            result = self._run_turn(turn)
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


def run_twenty_questions(
    *,
    name: str,
    client: LLMClient,
    agent_system: str,
    first_user_message: str,
    sim_system: str,
    turn_limit: int = 20,
    output_dir: Path,
) -> RunSummary:
    """Run a 20 Questions eval.

    Agent asks questions (text only). Simulator answers via tool calls.
    Game ends when sim calls correct_answer or turns run out.
    """
    runner = _TwentyQuestionsRunner(name=name, client=client, agent_system=agent_system, sim_system=sim_system)
    return runner.run(first_user_message=first_user_message, turn_limit=turn_limit, output_dir=output_dir)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    p = argparse.ArgumentParser(description="Twenty Questions eval")
    add_common_args(p)
    p.add_argument("--variant", choices=list(VARIANTS), required=True)
    args = p.parse_args()

    v = VARIANTS[args.variant]
    name = f"20q_{args.variant}"

    skill_text = load_skill()
    agent_system = build_agent_system(skill_text)
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

    summary = run_twenty_questions(
        name=name,
        client=client,
        agent_system=agent_system,
        first_user_message=first_user_message,
        sim_system=sim_system,
        turn_limit=v.turn_limit,
        output_dir=output_dir,
    )
    logger.info("%s", summary)


if __name__ == "__main__":
    main()
