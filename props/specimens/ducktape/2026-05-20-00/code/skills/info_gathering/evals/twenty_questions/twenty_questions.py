"""Twenty Questions eval variants.

Usage:
  bazel run //skills/info_gathering/evals/twenty_questions:twenty_questions_bin -- --variant states
  bazel run //skills/info_gathering/evals/twenty_questions:twenty_questions_bin -- \\
    --variant states --model gpt-oss:20b --base-url https://ollama.allegedly.works/v1
"""

import argparse
import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from agent_core.agent import Agent
from agent_core.direct_provider import DirectToolProvider
from agent_core.events import AssistantText, Response, ToolCall
from agent_core.handler import BaseHandler
from agent_core.loop_control import AllowAnyToolOrTextMessage, RequireAnyTool
from agent_core.tool_provider import ToolProvider
from openai_utils.model import OpenAIModelProto, SystemMessage, UserMessage
from skills.info_gathering.evals.docker_scratch import load_scratch_image, scratch_container
from skills.info_gathering.evals.harness import (
    add_common_args,
    build_agent_system,
    load_skill,
    model_from_args,
    output_dir_from_args,
    run_output_paths,
    save_summary,
)
from skills.info_gathering.evals.twenty_questions.prompts import (
    first_user_message as build_first_user_message,
    load_scratch_system_note,
    load_sim_prompt,
)
from skills.info_gathering.evals.twenty_questions.result_types import Correct, LogEntry, RunSummary, Timeout

logger = logging.getLogger(__name__)

# Safety cap: max scratch tool call rounds per agent turn before we give up.
_MAX_SCRATCH_STEPS = 20


class AnswerInput(BaseModel):
    response: Literal["yes", "no", "sort_of"]
    model_config = ConfigDict(extra="forbid")


class SimAnswer(BaseModel):
    kind: Literal["answer"] = "answer"
    response: Literal["yes", "no", "sort_of"]


class SimCorrectAnswer(BaseModel):
    kind: Literal["correct_answer"] = "correct_answer"


SimAction = SimAnswer | SimCorrectAnswer


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


class _TextCaptureHandler(BaseHandler):
    """Captures assistant text produced during an agent step."""

    def __init__(self) -> None:
        self._text: str | None = None

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        self._text = (self._text or "") + evt.text

    def take(self) -> str | None:
        """Return captured text and reset state."""
        text = self._text
        self._text = None
        return text.strip() if text else None


class _TurnLogHandler(BaseHandler):
    """Logs a LogEntry per LLM response."""

    def __init__(self, *, player: Literal["guesser", "simulator"], write_entry: Callable[[LogEntry], None]) -> None:
        self._player = player
        self._write_entry = write_entry
        self._text = ""
        self._tool_calls: list[ToolCall] = []

    def on_assistant_text_event(self, evt: AssistantText) -> None:
        self._text += evt.text

    def on_tool_call_event(self, evt: ToolCall) -> None:
        self._tool_calls.append(evt)

    def on_response(self, evt: Response) -> None:
        self._write_entry(
            LogEntry(
                timestamp=datetime.now(UTC),
                player=self._player,
                model=evt.model,
                content=self._text,
                tool_calls=[{"name": tc.name, "args": tc.args_json, "call_id": tc.call_id} for tc in self._tool_calls],
            )
        )
        self._text = ""
        self._tool_calls = []


class _TwentyQuestionsRunner:
    """Runs a single 20Q eval game with two Agent instances communicating in a loop."""

    def __init__(
        self,
        *,
        name: str,
        model: OpenAIModelProto,
        agent_system: str,
        sim_system: str,
        agent_tool_provider: ToolProvider,
    ) -> None:
        self.name = name
        self.model = model
        self._agent_system = agent_system
        self._sim_system = sim_system
        self._agent_tool_provider = agent_tool_provider

    async def run(self, *, first_user_message: str, turn_limit: int, output_dir: Path) -> RunSummary:
        """Run the full game loop and return summary."""
        calls_path, summary_path = run_output_paths(self.name, output_dir)

        with calls_path.open("w") as calls_file:

            def write_entry(entry: LogEntry) -> None:
                calls_file.write(entry.model_dump_json() + "\n")
                calls_file.flush()

            summary = await self._run_game(
                first_user_message=first_user_message, turn_limit=turn_limit, write_entry=write_entry
            )

        save_summary(summary=summary, summary_path=summary_path)
        return summary

    async def _run_game(
        self, *, first_user_message: str, turn_limit: int, write_entry: Callable[[LogEntry], None]
    ) -> RunSummary:
        # Sim tool provider: closures capture sim_action
        sim_action: SimAction | None = None
        sim_provider = DirectToolProvider()

        @sim_provider.tool
        def answer(args: AnswerInput) -> None:
            """Answer the player's yes/no question."""
            nonlocal sim_action
            sim_action = SimAnswer(response=args.response)

        @sim_provider.tool
        def correct_answer() -> None:
            """The player correctly guessed the secret."""
            nonlocal sim_action
            sim_action = SimCorrectAnswer()

        agent_log = _TurnLogHandler(player="guesser", write_entry=write_entry)
        sim_log = _TurnLogHandler(player="simulator", write_entry=write_entry)
        text_capture = _TextCaptureHandler()

        agent = Agent(
            tool_provider=self._agent_tool_provider,
            client=self.model,
            parallel_tool_calls=False,
            handlers=[agent_log, text_capture],
            tool_policy=AllowAnyToolOrTextMessage(),
        )
        agent.process_message(SystemMessage.text(self._agent_system))

        sim = Agent(
            tool_provider=sim_provider,
            client=self.model,
            parallel_tool_calls=False,
            handlers=[sim_log],
            tool_policy=RequireAnyTool(),
        )
        sim.process_message(SystemMessage.text(self._sim_system))

        async def agent_turn() -> str | None:
            for _ in range(_MAX_SCRATCH_STEPS):
                await agent.step()
                text = text_capture.take()
                if text:
                    return text
            logger.warning("Agent hit scratch step limit without producing text")
            return None

        async def sim_turn(question: str) -> SimAction | None:
            nonlocal sim_action
            sim_action = None
            sim.process_message(UserMessage.text(question))
            await sim.step()
            if sim_action is None:
                logger.warning("Sim step produced no action (tool_choice=required was ignored)")
            return sim_action

        agent.process_message(UserMessage.text(first_user_message))

        result: Correct | Timeout = Timeout(limit=turn_limit)
        turn = 0
        for turn in range(1, turn_limit + 1):
            logger.info("Turn %d...", turn)

            agent_text = await agent_turn()
            if not agent_text:
                break

            action = await sim_turn(agent_text)
            if action is None:
                break

            if isinstance(action, SimCorrectAnswer):
                result = Correct(turns=turn)
                break

            agent.process_message(UserMessage.text(action.response))

        return RunSummary(
            eval_name=self.name, framework="agent_core", model=self.model.model, api="openai", turns=turn, result=result
        )


async def run_twenty_questions(
    *,
    name: str,
    model: OpenAIModelProto,
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
        model=model,
        agent_system=agent_system,
        sim_system=sim_system,
        agent_tool_provider=agent_tool_provider,
    )
    return await runner.run(first_user_message=first_user_message, turn_limit=turn_limit, output_dir=output_dir)


async def _async_main(args: argparse.Namespace) -> None:
    v = VARIANTS[args.variant]
    name = f"20q_{args.variant}"

    skill_text = load_skill()
    scratch_note = load_scratch_system_note()
    agent_system = build_agent_system(skill_text, extra_system=scratch_note)
    model = model_from_args(args)
    output_dir = output_dir_from_args(args)

    sim_system = load_sim_prompt(secret=v.secret, turn_limit=v.turn_limit)

    first_user_message = build_first_user_message(domain_description=v.domain_description, turn_limit=v.turn_limit)

    logger.info("=" * 60)
    logger.info("  %s  |  %s", name, model.model)
    logger.info("=" * 60)

    image = load_scratch_image()
    async with scratch_container(image) as provider:
        summary = await run_twenty_questions(
            name=name,
            model=model,
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
