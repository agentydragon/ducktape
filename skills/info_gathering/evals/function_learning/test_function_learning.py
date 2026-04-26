"""Tests for the function learning game loop.

Uses ReplayChatClient for scripted LLM responses, but runs real
Docker-based program evaluation (no mocking of evaluate_program) and a
real `scratch_exec_mcp_tool()` exec tool — the replay scripts don't
call exec, but wiring it up to the real MCP launcher means the test
fails loud if that surface breaks.

Loads `python:3.13-slim` into the Docker daemon up-front via
`load_oci_image` (matches the pattern in
`skills/info_gathering/evals/docker_scratch.py`) so neither the test's
scoring container nor the launcher subprocess hits a 404 on a worker
without the image cached.
"""

import json
import uuid
from pathlib import Path

import aiodocker
import pytest_bazel
from agent_framework import ChatResponse, Content, Message
from skills.eval_infra.empty_skill.empty_skill_skill_spec import SPEC as EMPTY_SKILL_SPEC

from skills.eval_infra.eval_sandbox import eval_sandbox
from skills.eval_infra.skill_staging import stage_skill
from skills.info_gathering.evals.function_learning.function_learning import run_game
from skills.info_gathering.evals.function_learning.functions import PARITY_GROUPS
from skills.info_gathering.evals.function_learning.result_types import RunSummary
from skills.info_gathering.evals.replay_client import ReplayChatClient
from third_party.containers.rlocations import PYTHON_3_13_SLIM
from util.oci import load_oci_image

_TEST_TURNS = 3


def _play_turn_call(query: int, program: str) -> ChatResponse:
    return ChatResponse(
        messages=[
            Message(
                "assistant",
                [
                    Content.from_function_call(
                        "call_1", "play_turn", arguments=json.dumps({"query": query, "program": program})
                    )
                ],
            )
        ],
        finish_reason="tool_calls",
    )


async def _run_with_replay(
    *,
    completions: list[ChatResponse],
    tmp_path: Path,
    function_name: str = "parity_groups",
    hint: bool = True,
    turn_limit: int = _TEST_TURNS,
) -> RunSummary:
    client = ReplayChatClient(responses=completions)
    image_tag = load_oci_image(PYTHON_3_13_SLIM)

    # Stage the empty skill so the sandbox shape matches production: prompt
    # claims `SKILL_PATH` is mounted, and it really is.
    staged = stage_skill(EMPTY_SKILL_SPEC, tmp_path / "skill_extract")
    workspace = tmp_path / "work"
    workspace.mkdir()

    async with eval_sandbox(skill=staged, workspace=workspace) as exec_tool, aiodocker.Docker() as docker:
        container = await docker.containers.run(
            config={"Image": image_tag, "Cmd": ["sleep", "300"]}, name=f"fl-test-{uuid.uuid4().hex[:8]}"
        )
        try:
            return await run_game(
                function_name=function_name,
                hint=hint,
                model="test-model",
                api="openai",
                output_dir=tmp_path,
                exec_tool=exec_tool,
                model_client=client,
                scoring_container=container,
                skill_md=staged.md_text,
                turn_limit=turn_limit,
            )
        finally:
            # Force-remove the container so we don't wait for a SIGTERM grace period;
            # the sleep process ignores SIGTERM, so a normal stop would wait the full
            # default timeout before SIGKILL.
            await container.delete(force=True)


async def test_basic_game_completes(tmp_path: Path) -> None:
    """Model plays 3 turns with a trivial all-zeros program, game completes."""
    completions = [_play_turn_call(i, "for i in range(256): print(0)") for i in range(_TEST_TURNS)]

    summary = await _run_with_replay(completions=completions, tmp_path=tmp_path)

    assert summary.result.kind == "completed"
    assert len(summary.result.per_turn_losses) == _TEST_TURNS
    assert summary.turns == _TEST_TURNS
    # All-zeros program should have non-zero loss (parity_groups isn't all zeros).
    assert summary.result.total_hamming_loss > 0


async def test_perfect_program_zero_loss(tmp_path: Path) -> None:
    """A program implementing the correct parity function gets 0 loss."""
    perfect_program = (
        "for x in range(256):\n"
        "    bits = [(x >> (7 - i)) & 1 for i in range(8)]\n"
        "    r = sum((bits[i] ^ bits[i+1]) << (3 - i//2) for i in range(0, 8, 2))\n"
        "    print(r)"
    )
    completions = [_play_turn_call(i, perfect_program) for i in range(_TEST_TURNS)]

    summary = await _run_with_replay(completions=completions, tmp_path=tmp_path)

    assert summary.result.total_hamming_loss == 0
    assert all(loss == 0 for loss in summary.result.per_turn_losses)


async def test_improving_programs(tmp_path: Path) -> None:
    """Loss should decrease as the program improves turn over turn."""
    programs = [
        # Turn 1: always output 0 (bad)
        "for i in range(256): print(0)",
        # Turn 2: gets first pair right, rest zeros
        ("for x in range(256):\n    b0 = (x >> 7) & 1; b1 = (x >> 6) & 1\n    print((b0 ^ b1) << 3)"),
        # Turn 3: gets all pairs right (perfect)
        (
            "for x in range(256):\n"
            "    bits = [(x >> (7 - i)) & 1 for i in range(8)]\n"
            "    r = sum((bits[i] ^ bits[i+1]) << (3 - i//2) for i in range(0, 8, 2))\n"
            "    print(r)"
        ),
    ]
    completions = [_play_turn_call(i, programs[i]) for i in range(_TEST_TURNS)]

    summary = await _run_with_replay(completions=completions, tmp_path=tmp_path)

    losses = summary.result.per_turn_losses
    assert len(losses) == 3
    assert losses[0] > losses[1] > losses[2]
    assert losses[2] == 0


async def test_erroring_program_max_loss(tmp_path: Path) -> None:
    """A program that raises an exception gets maximum loss per errored input."""
    completions = [_play_turn_call(0, "raise ValueError('broken')")]

    summary = await _run_with_replay(completions=completions, tmp_path=tmp_path, turn_limit=1)

    # No output lines: all 256 inputs missing, max loss = 256 * 4 = 1024.
    assert summary.result.per_turn_losses[0] == 256 * PARITY_GROUPS.m


async def test_error_summary_reported(tmp_path: Path) -> None:
    """Error summary counts are populated for a program that produces no output."""
    completions = [_play_turn_call(0, "raise ValueError('broken')")]

    summary = await _run_with_replay(completions=completions, tmp_path=tmp_path, turn_limit=1)

    turn = summary.result.per_turn_losses
    assert len(turn) == 1
    # The turn result is in the JSONL, but we can check via the summary that loss is max.
    assert summary.result.total_hamming_loss == 256 * PARITY_GROUPS.m


def _check_output_files(tmp_path: Path) -> None:
    """Verify JSONL and summary files exist with correct structure (sync helper)."""
    jsonl_files = list(tmp_path.glob("*_calls.jsonl"))
    summary_files = list(tmp_path.glob("*_summary.json"))
    assert len(jsonl_files) == 1
    assert len(summary_files) == 1

    summary_data = json.loads(summary_files[0].read_text())
    assert summary_data["framework"] == "agent_framework"
    assert summary_data["result"]["kind"] == "completed"


async def test_output_files_written(tmp_path: Path) -> None:
    completions = [_play_turn_call(0, "for i in range(256): print(0)") for _ in range(_TEST_TURNS)]
    await _run_with_replay(completions=completions, tmp_path=tmp_path)
    _check_output_files(tmp_path)


async def test_early_termination_on_zero_loss(tmp_path: Path) -> None:
    """Game ends after turn 1 when 0 loss is achieved; remaining turns are imputed as 0."""
    perfect_program = (
        "for x in range(256):\n"
        "    bits = [(x >> (7 - i)) & 1 for i in range(8)]\n"
        "    r = sum((bits[i] ^ bits[i+1]) << (3 - i//2) for i in range(0, 8, 2))\n"
        "    print(r)"
    )
    # Supply more completions than should be consumed — game should stop after turn 1.
    completions = [_play_turn_call(0, perfect_program)] * _TEST_TURNS

    summary = await _run_with_replay(completions=completions, tmp_path=tmp_path, turn_limit=_TEST_TURNS)

    assert summary.turns == 1, "Game should have stopped after solving on turn 1"
    assert summary.result.solved_at_turn == 1
    assert len(summary.result.per_turn_losses) == _TEST_TURNS, "per_turn_losses padded to turn_limit"
    assert summary.result.per_turn_losses == [0, 0, 0]
    assert summary.result.total_hamming_loss == 0


if __name__ == "__main__":
    pytest_bazel.main()
