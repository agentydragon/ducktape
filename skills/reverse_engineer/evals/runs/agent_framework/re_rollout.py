"""Single-rollout reverse-engineering eval against go_crypto_server.

Hands a Haiku agent the garbled go_crypto_server binary plus the
reverse_engineer skill, gives it shell access via MCP exec inside a stock
python:3.13-slim container, runs for up to 10 minutes, and writes the
transcript + the agent's /work workspace to --output-dir.

`Agent.run()` drives the tool-dispatch loop. JSONL transcript writes go
through `JsonlTranscriptProvider` (AF's standard `HistoryProvider`-shaped
audit log via `Message.to_json()`). The `submit` tool sets
`SubmitState.summary`; `terminate_when` raises `MiddlewareTermination`
once that is set. Wall-clock enforcement comes from `asyncio.wait_for`
around `agent.run()`.

No assertions about the agent's output — this is a manual-inspection
harness. Outputs:
  <output-dir>/transcript_<variant>.jsonl    — AF Message stream
  <output-dir>/work_<variant>/               — host-side bind of /work
  <output-dir>/summary_<variant>.json        — end_reason, wall_seconds, submit text

Run with:
  bb run --remote_executor="" \\
    //skills/reverse_engineer/evals/runs/agent_framework:re_rollout -- \\
    --skill on --output-dir /tmp/re_eval/$(date -u +%Y%m%dT%H%M%SZ)
"""

import argparse
import asyncio
import logging
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Literal

from agent_framework import Agent, AgentSession, FunctionTool, Message
from pydantic import BaseModel, Field
from skills.eval_infra.empty_skill.empty_skill_skill_spec import SPEC as EMPTY_SKILL_SPEC
from skills.reverse_engineer.reverse_engineer_skill_spec import SPEC as REVERSE_ENGINEER_SKILL_SPEC

from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel
from skills.eval_infra.af_chat_client import build_model_client
from skills.eval_infra.eval_prompt import compose_system_prompt
from skills.eval_infra.eval_sandbox import INPUT_PATH, WORK_PATH, eval_sandbox
from skills.eval_infra.skill_staging import SkillSpec, stage_skill
from skills.eval_infra.termination import terminate_when
from skills.eval_infra.transcript import JsonlTranscriptProvider
from util.bazel.runfiles import get_required_path

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_DEFAULT_MAX_STEPS = 1000
_DEFAULT_WALL_TIMEOUT_SECONDS = 600

_TARGET_BINARY_RLOCATION = "_main/skills/reverse_engineer/evals/specimens/go_crypto_server/go_crypto_server_garbled.bin"

# Maps the --skill CLI value to a SkillSpec. The "off" arm uses an empty
# SKILL.md so the sandbox shape is uniform across arms — there is no
# `if skill_on:` branch.
_SKILL_BY_ARM: dict[str, SkillSpec] = {"on": REVERSE_ENGINEER_SKILL_SPEC, "off": EMPTY_SKILL_SPEC}


# -- Run summary --


class RunSummary(BaseModel):
    model: str
    skill_on: bool
    end_reason: Literal["submit", "agent_returned", "wall_timeout"]
    wall_seconds: float
    submit_summary: str | None = None


# -- Prompts --


_TARGET_PATH = INPUT_PATH / "target"


# All RE-specific task framing lives in the first user message — the system
# prompt carries only the shared skill block + exec-tool note from
# `compose_system_prompt`. Wall-clock and per-turn budgets are enforced by
# the harness (asyncio.wait_for around agent.run; AF caps consecutive tool
# calls) and are NOT disclosed to the agent to avoid biasing it toward
# early submission.
_FIRST_USER_MESSAGE = (
    f"Reverse-engineer the stripped Go binary at {_TARGET_PATH} into a complete, "
    f"human-readable Go source tree under {WORK_PATH}/ (your working directory). "
    "The recovered source should compile and be behaviorally equivalent to the binary — "
    "same protocol, same endpoints, same crypto/encoding/MAC/storage semantics — not "
    "stubs, placeholders, or TODOs. Pick reasonable, idiomatic Go names where the "
    "binary's were obfuscated. When done, call `submit` with a one-paragraph summary "
    "noting which parts you are confident vs unsure about."
)


# -- Submit state + tool --


class _SubmitState:
    summary: str | None = None


# Strict-mode-compatible input schema (additionalProperties: false, summary
# in `required`) so the submit tool can ride alongside `exec` under
# Anthropic's grammar-constrained tool-use mode. Without this, AF's
# default schema-from-signature would emit a non-strict shape and the
# whole request would 400 once strict mode is engaged.
class _SubmitInput(OpenAIStrictModeBaseModel):
    summary: str = Field(description="One-paragraph summary noting which parts you are confident vs unsure about.")


def _make_submit_tool(state: _SubmitState) -> FunctionTool:
    async def submit(summary: str) -> str:
        """Mark the rollout complete with a final summary."""
        state.summary = summary
        return "Submission recorded. The session will end."

    return FunctionTool(
        name="submit",
        description="Call when reverse engineering is complete (or you are stuck).",
        func=submit,
        input_model=_SubmitInput,
    )


# -- Main --


async def _async_main(args: argparse.Namespace) -> None:
    skill_on = args.skill == "on"
    suffix = "skill_on" if skill_on else "skill_off"
    out_dir: Path = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    workspace = out_dir / f"work_{suffix}"
    transcript_path = out_dir / f"transcript_{suffix}.jsonl"
    summary_path = out_dir / f"summary_{suffix}.json"

    # Assemble the inputs dir: copy the runfiles target binary in under the
    # name the prompt promises (`/input/target`). Per-run dir so concurrent
    # rollouts don't collide.
    inputs_dir = out_dir / f"inputs_{suffix}"
    inputs_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(get_required_path(_TARGET_BINARY_RLOCATION), inputs_dir / "target")

    staged = stage_skill(_SKILL_BY_ARM[args.skill], out_dir / f"skill_extract_{suffix}")
    system_prompt = compose_system_prompt(skill_md=staged.md_text)

    submit_state = _SubmitState()
    submit_tool = _make_submit_tool(submit_state)

    model_client = build_model_client(
        api="anthropic",
        model=args.model,
        function_invocation_configuration={
            "max_iterations": args.max_steps,
            # AF defaults to a terse "Error: Argument parsing failed." for tool-arg
            # validation errors, stashing the actual reason on Content.exception
            # but never relaying it to the model. Flipping this on inlines the
            # detail (e.g. "Missing required argument(s) for 'exec': cmd, timeout_ms")
            # so the model has something concrete to self-correct against. See
            # agent_framework/_tools.py:1387-1396.
            "include_detailed_errors": True,
        },
        # Anthropic's grammar-constrained tool-use mode — ``strict: true`` on every
        # custom tool. Makes a malformed call like ``exec({})`` literally impossible
        # for the model to emit, rather than only post-hoc-rejected by Pydantic.
        # Requires every tool's input_schema to be strict-compatible
        # (additionalProperties: false, all fields in `required`); _SubmitInput
        # above and the exec MCP tool both already use OpenAIStrictModeBaseModel.
        strict_tools=True,
    )
    t_start = time.monotonic()
    end_reason: Literal["submit", "agent_returned", "wall_timeout"]

    with JsonlTranscriptProvider.opened(transcript_path) as transcript:
        async with eval_sandbox(skill=staged, workspace=workspace, inputs=inputs_dir) as exec_tool:
            agent = Agent(
                client=model_client,
                tools=[exec_tool, submit_tool],
                context_providers=[transcript],
                middleware=[terminate_when(lambda: submit_state.summary is not None, reason="submit called")],
                default_options={"tool_choice": "required", "allow_multiple_tool_calls": False},
                require_per_service_call_history_persistence=True,
            )
            try:
                # `terminate_when` raises `MiddlewareTermination` once submit is
                # called; AF's tool loop catches it internally so `agent.run`
                # returns normally.
                await asyncio.wait_for(
                    agent.run(
                        [Message("system", [system_prompt]), Message("user", [_FIRST_USER_MESSAGE])],
                        session=AgentSession(),
                    ),
                    timeout=args.wall_timeout,
                )
                # `agent_returned` is the catch-all when `agent.run()` returns
                # without `submit` being called. It can be hit because the model
                # stopped emitting tool calls, AF auto-stopped after consecutive
                # tool errors, or `max_iterations` was reached -- AF doesn't
                # surface that distinction. Inspect the transcript to tell.
                end_reason = "submit" if submit_state.summary is not None else "agent_returned"
            except TimeoutError:
                end_reason = "wall_timeout"

    summary = RunSummary(
        model=args.model,
        skill_on=skill_on,
        end_reason=end_reason,
        wall_seconds=round(time.monotonic() - t_start, 2),
        submit_summary=submit_state.summary,
    )
    summary_path.write_text(summary.model_dump_json(indent=2))
    logger.info("Rollout finished. Output dir: %s", out_dir)
    logger.info("Summary: %s", summary.model_dump_json())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skill", choices=["on", "off"], default="on")
    parser.add_argument(
        "--output-dir", type=Path, required=True, help="Directory for transcript, work/, summary. Created if missing."
    )
    parser.add_argument("--model", default=_DEFAULT_MODEL)
    parser.add_argument("--max-steps", type=int, default=_DEFAULT_MAX_STEPS)
    parser.add_argument(
        "--wall-timeout", type=int, default=_DEFAULT_WALL_TIMEOUT_SECONDS, help="Agent wall-clock budget in seconds."
    )
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set; refusing to run.")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
