"""Twenty Questions eval variants.

Usage:
  bazel run //nix/home/skills/info_gathering/evals/twenty_questions:twenty_questions_bin -- --variant states
"""

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import anthropic
import anthropic.types
from pydantic import BaseModel

from nix.home.skills.info_gathering.evals.harness import (
    LogEntry,
    RunSummary,
    TokenTracker,
    add_common_args,
    build_agent_system,
    call_api,
    extract_text,
    extract_tool_calls,
    load_skill,
    log_response,
    make_client,
    output_dir_from_args,
    save_results,
    thinking_from_args,
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

SIM_TOOLS: list[anthropic.types.ToolParam] = [ANSWER_TOOL, CORRECT_ANSWER_TOOL]
SIM_TOOL_CHOICE: anthropic.types.ToolChoiceParam = anthropic.types.ToolChoiceAnyParam(
    type="any", disable_parallel_tool_use=True
)


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


def run_twenty_questions(
    *,
    name: str,
    client: anthropic.Anthropic,
    model: str,
    agent_system: str,
    first_user_message: str,
    sim_system: str,
    turn_limit: int = 20,
    thinking_budget: int | None = None,
    output_dir: Path,
) -> RunSummary:
    """Run a 20 Questions eval.

    Agent asks questions (text only). Simulator answers via tools (forced by
    tool_choice=any). Game ends when sim calls correct_answer or turns run out.
    """
    tracker = TokenTracker(model=model)
    log_entries: list[LogEntry] = []

    agent_messages: list[anthropic.types.MessageParam] = [{"role": "user", "content": first_user_message}]
    sim_messages: list[anthropic.types.MessageParam] = []
    last_tc_id: str | None = None
    result: Correct | Timeout

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

        # Sim turn: tool result from previous turn (if any) + agent's question
        if last_tc_id:
            content: str | list = [
                {"type": "tool_result", "tool_use_id": last_tc_id, "content": "ok"},
                {"type": "text", "text": agent_text},
            ]
        else:
            content = agent_text
        last_tc_id = None

        sim_messages.append({"role": "user", "content": content})
        sim_resp = call_api(
            client=client,
            messages=sim_messages,
            system=sim_system,
            model=model,
            tools=SIM_TOOLS,
            tool_choice=SIM_TOOL_CHOICE,
            thinking_budget=thinking_budget,
        )
        tracker.add(sim_resp.usage)
        log_response(log_entries, name=name, player="simulator", turn=turn, model=model, response=sim_resp)
        sim_messages.append({"role": "assistant", "content": sim_resp.content})

        (tc,) = extract_tool_calls(sim_resp)
        assert isinstance(tc.input, dict)

        if tc.name == "correct_answer":
            result = Correct(turns=turn)
            break

        # answer tool
        answer = AnswerInput.model_validate(tc.input)
        last_tc_id = tc.id
        agent_messages.append({"role": "user", "content": answer.response})
    else:
        result = Timeout(limit=turn_limit)

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
    client = make_client()
    thinking = thinking_from_args(args)
    output_dir = output_dir_from_args(args)

    sim_template = get_required_path(_SIM_RLOCATION).read_text()
    sim_system = sim_template.format(secret=v.secret, turn_limit=v.turn_limit)

    first_user_message = (
        f"Play 20 Questions. I'm thinking of {v.domain_description}. "
        f"You have {v.turn_limit} yes/no questions. "
        "When confident, state: 'My answer is: [X]'."
    )

    logger.info("=" * 60)
    logger.info("  %s  |  %s  |  thinking=%s", name, args.model, thinking or "off")
    logger.info("=" * 60)

    summary = run_twenty_questions(
        name=name,
        client=client,
        model=args.model,
        agent_system=agent_system,
        first_user_message=first_user_message,
        sim_system=sim_system,
        turn_limit=v.turn_limit,
        thinking_budget=thinking,
        output_dir=output_dir,
    )
    logger.info("%s", summary)


if __name__ == "__main__":
    main()
