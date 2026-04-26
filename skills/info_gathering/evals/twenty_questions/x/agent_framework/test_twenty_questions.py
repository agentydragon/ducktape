"""Tests for Twenty Questions Agent Framework implementation with structured guesser tools.

Uses ReplayChatClient to run games with scripted model responses.
The guesser calls ask_yes_no_question/guess_answer tools (which invoke the
simulator inline), so the replay sequence is:
  guesser_tool_call, simulator_tool_call, guesser_tool_call, simulator_tool_call, ...
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import pytest_bazel
from agent_framework import ChatResponse, Content, Message
from skills.eval_infra.empty_skill.empty_skill_skill_spec import SPEC as EMPTY_SKILL_SPEC

from skills.eval_infra.eval_sandbox import eval_sandbox
from skills.eval_infra.skill_staging import stage_skill
from skills.info_gathering.evals.replay_client import ReplayChatClient
from skills.info_gathering.evals.twenty_questions.result_types import RunSummary
from skills.info_gathering.evals.twenty_questions.x.agent_framework.twenty_questions import run_game
from third_party.containers.rlocations import PYTHON_3_13_SLIM
from util.oci import load_oci_image


def _tool_call_reply(name: str, arguments: dict[str, object]) -> ChatResponse:
    """Build a ChatResponse with a single function call."""
    return ChatResponse(
        messages=[Message("assistant", [Content.from_function_call("call_1", name, arguments=json.dumps(arguments))])],
        finish_reason="tool_calls",
    )


def _guesser_ask(question: str) -> ChatResponse:
    """Guesser calls ask_yes_no_question."""
    return _tool_call_reply("ask_yes_no_question", {"question": question})


def _guesser_guess(answer: str) -> ChatResponse:
    """Guesser calls guess_answer."""
    return _tool_call_reply("guess_answer", {"answer": answer})


def _sim_answer(response: str) -> ChatResponse:
    """Simulator calls answer."""
    return _tool_call_reply("answer", {"response": response})


def _sim_correct() -> ChatResponse:
    """Simulator calls correct_answer."""
    return _tool_call_reply("correct_answer", {})


def _sim_invalid(reason: str) -> ChatResponse:
    """Simulator calls invalid_input."""
    return _tool_call_reply("invalid_input", {"reason": reason})


@pytest.fixture
def _patch_prompts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "skills.info_gathering.evals.twenty_questions.x.agent_framework.twenty_questions.load_sim_prompt",
        MagicMock(return_value="You are the simulator."),
    )


async def _run_with_replay(
    *, completions: list[ChatResponse], tmp_path: Path, variant_name: str = "states"
) -> RunSummary:
    """Run a game with scripted responses.

    Completions interleave guesser and simulator:
    [guesser_tool_call_1, simulator_tool_call_1, guesser_tool_call_2, simulator_tool_call_2, ...]

    Stages the empty skill and binds it into a real `eval_sandbox` so the
    test mirrors production: the agent sees a populated `SKILL_PATH` and
    an exec MCP tool wired to a real launcher subprocess. Replay scripts
    don't call exec, but the wiring fails loud if the launcher / MCP
    layer breaks.
    """
    client = ReplayChatClient(responses=completions)
    load_oci_image(PYTHON_3_13_SLIM)

    staged = stage_skill(EMPTY_SKILL_SPEC, tmp_path / "skill_extract")
    workspace = tmp_path / "work"
    workspace.mkdir()

    async with eval_sandbox(skill=staged, workspace=workspace, inputs=None) as exec_tool:
        return await run_game(
            variant_name=variant_name,
            model="test-model",
            api="openai",
            output_dir=tmp_path,
            model_client=client,
            skill_md=staged.md_text,
            exec_tool=exec_tool,
        )


@pytest.mark.usefixtures("_patch_prompts")
async def test_correct_guess(tmp_path: Path) -> None:
    """Guesser asks a question then guesses correctly on turn 2."""
    summary = await _run_with_replay(
        completions=[_guesser_ask("Is it a place?"), _sim_answer("yes"), _guesser_guess("New Mexico"), _sim_correct()],
        tmp_path=tmp_path,
    )

    assert summary.result.kind == "correct"
    assert summary.result.turns == 2
    assert summary.turns == 2
    assert summary.framework == "agent_framework"


@pytest.mark.usefixtures("_patch_prompts")
async def test_timeout(tmp_path: Path) -> None:
    """Guesser never guesses correctly and hits the turn limit."""
    completions: list[ChatResponse] = []
    for i in range(1, 21):
        completions.append(_guesser_ask(f"Question {i}?"))
        completions.append(_sim_answer("no"))

    summary = await _run_with_replay(completions=completions, tmp_path=tmp_path)

    assert summary.result.kind == "timeout"
    assert summary.result.limit == 20
    assert summary.turns == 20


@pytest.mark.usefixtures("_patch_prompts")
async def test_correct_on_first_turn(tmp_path: Path) -> None:
    """Guesser guesses correctly immediately on turn 1."""
    summary = await _run_with_replay(completions=[_guesser_guess("New Mexico"), _sim_correct()], tmp_path=tmp_path)

    assert summary.result.kind == "correct"
    assert summary.result.turns == 1
    assert summary.turns == 1


@pytest.mark.usefixtures("_patch_prompts")
async def test_sort_of_response(tmp_path: Path) -> None:
    """Simulator responds with sort_of, game continues."""
    summary = await _run_with_replay(
        completions=[_guesser_ask("Is it hot?"), _sim_answer("sort_of"), _guesser_guess("New Mexico"), _sim_correct()],
        tmp_path=tmp_path,
    )

    assert summary.result.kind == "correct"
    assert summary.result.turns == 2


@pytest.mark.usefixtures("_patch_prompts")
async def test_invalid_input_does_not_consume_turn(tmp_path: Path) -> None:
    """Simulator returns invalid_input, turn is refunded."""
    summary = await _run_with_replay(
        completions=[
            _guesser_ask("Tell me about the state"),  # Not a yes/no question
            _sim_invalid("Not a yes/no question"),
            _guesser_ask("Is it west of the Mississippi?"),
            _sim_answer("yes"),
            _guesser_guess("New Mexico"),
            _sim_correct(),
        ],
        tmp_path=tmp_path,
    )

    assert summary.result.kind == "correct"
    assert summary.result.turns == 2  # Invalid input didn't count
    assert summary.invalid_input_count == 1


def _check_output_files(tmp_path: Path) -> None:
    """Verify JSONL and summary files are written with correct content (sync helper)."""
    jsonl_files = list(tmp_path.glob("*_calls.jsonl"))
    summary_files = list(tmp_path.glob("*_summary.json"))
    assert len(jsonl_files) == 1
    assert len(summary_files) == 1

    lines = jsonl_files[0].read_text().strip().split("\n")
    assert len(lines) >= 2

    summary_data = json.loads(summary_files[0].read_text())
    assert summary_data["framework"] == "agent_framework"
    assert summary_data["result"]["kind"] == "correct"


@pytest.mark.usefixtures("_patch_prompts")
async def test_log_files_written(tmp_path: Path) -> None:
    """JSONL and summary files are written with correct content."""
    await _run_with_replay(completions=[_guesser_guess("New Mexico"), _sim_correct()], tmp_path=tmp_path)
    _check_output_files(tmp_path)


if __name__ == "__main__":
    pytest_bazel.main()
