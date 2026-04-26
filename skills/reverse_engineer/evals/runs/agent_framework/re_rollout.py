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
import contextlib
import logging
import os
import sys
import time
from pathlib import Path
from typing import Literal

from agent_framework import Agent, AgentSession, FunctionTool, Message, MiddlewareTermination
from pydantic import BaseModel
from skills.eval_infra.empty_skill.empty_skill_skill_spec import SPEC as EMPTY_SKILL_SPEC
from skills.reverse_engineer.reverse_engineer_skill_spec import SPEC as REVERSE_ENGINEER_SKILL_SPEC

from skills.eval_infra.af_chat_client import build_model_client
from skills.eval_infra.eval_sandbox import INPUT_PATH, SKILL_PATH, WORK_PATH, eval_sandbox
from skills.eval_infra.skill_staging import SkillSpec, stage_skill
from skills.eval_infra.termination import terminate_when
from skills.eval_infra.transcript import JsonlTranscriptProvider
from util.bazel.runfiles import get_required_path

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_DEFAULT_MAX_STEPS = 200
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
    end_reason: Literal["submit", "step_cap", "wall_timeout", "error"]
    wall_seconds: float
    submit_summary: str | None = None
    error: str | None = None


# -- Prompts --


_TARGET_PATH = INPUT_PATH / "target"


def _build_system_prompt(*, skill_md_text: str) -> str:
    """Compose the RE system prompt.

    `skill_md_text` is inlined verbatim. The off-arm passes an empty string
    (the empty-skill tar's SKILL.md is blank), keeping the sandbox shape
    uniform across arms.
    """
    return (
        f"You are reverse-engineering a stripped Go binary located at {_TARGET_PATH}.\n"
        f"Recover its source as Go files under {WORK_PATH}/ (your working directory).\n"
        "You have shell access via the `exec` tool — `cmd` is a list of strings, no shell\n"
        "expansion. Stdout and stderr are returned together. Install whatever you need\n"
        "(apt-get install -y ..., pip install ..., curl ...). The container has internet.\n"
        "When you have gone as far as you can, call `submit` with a one-paragraph summary\n"
        "of what the program does and which parts you are confident vs unsure about.\n\n"
        f"A reverse-engineering skill is available. Its SKILL.md is included below verbatim.\n"
        f"The files SKILL.md references (e.g. examples/pclntool.go, examples/garble_re_recipe.sh)\n"
        f"live inside the container at {SKILL_PATH}/. You can read them with "
        f"`cat {SKILL_PATH}/...`, run scripts with `bash {SKILL_PATH}/examples/...`,\n"
        f"build Go helpers with `go run {SKILL_PATH}/examples/pclntool.go ...`, etc.\n\n"
        "--- BEGIN SKILL.md ---\n"
        f"{skill_md_text}\n"
        "--- END SKILL.md ---\n"
    )


_FIRST_USER_MESSAGE = (
    f"Reverse-engineer the binary at {_TARGET_PATH}. Recover its source as Go files under "
    f"{WORK_PATH}/. You have a 10-minute wall-clock budget and at most 200 turns. "
    "When done, call `submit` with a one-paragraph summary."
)


# -- Submit state + tool --


class _SubmitState:
    summary: str | None = None


def _make_submit_tool(state: _SubmitState) -> FunctionTool:
    async def submit(summary: str) -> str:
        """Mark the rollout complete with a final summary."""
        state.summary = summary
        return "Submission recorded. The session will end."

    return FunctionTool(
        name="submit",
        description=(
            "Call when reverse engineering is complete (or you are stuck). Pass a one-paragraph summary of the program."
        ),
        func=submit,
    )


# -- Main --


async def _async_main(args: argparse.Namespace) -> int:
    skill_on = args.skill == "on"
    suffix = "skill_on" if skill_on else "skill_off"
    out_dir: Path = args.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    workspace = out_dir / f"work_{suffix}"
    workspace.mkdir(parents=True, exist_ok=True)
    transcript_path = out_dir / f"transcript_{suffix}.jsonl"
    summary_path = out_dir / f"summary_{suffix}.json"

    target_path = get_required_path(_TARGET_BINARY_RLOCATION)

    staged = stage_skill(_SKILL_BY_ARM[args.skill], out_dir / f"skill_extract_{suffix}")
    system_prompt = _build_system_prompt(skill_md_text=staged.md_text)

    submit_state = _SubmitState()
    submit_tool = _make_submit_tool(submit_state)

    model_client = build_model_client(
        api="anthropic", model=args.model, function_invocation_configuration={"max_iterations": args.max_steps}
    )
    t_start = time.monotonic()
    error_msg: str | None = None
    end_reason: Literal["submit", "step_cap", "wall_timeout", "error"] = "error"

    try:
        with transcript_path.open("w") as transcript_f:
            # AF "instructions" don't flow through the Message stream — seed it
            # manually so the JSONL transcript has the full context at the top.
            transcript_f.write(Message("system", [system_prompt]).to_json() + "\n")
            transcript_f.flush()

            async with eval_sandbox(skill=staged, workspace=workspace, inputs={"target": target_path}) as exec_tool:
                agent = Agent(
                    client=model_client,
                    instructions=system_prompt,
                    tools=[exec_tool, submit_tool],
                    context_providers=[JsonlTranscriptProvider(transcript_f)],
                    middleware=[terminate_when(lambda: submit_state.summary is not None, reason="submit called")],
                    default_options={"tool_choice": "required", "allow_multiple_tool_calls": False},
                )
                try:
                    with contextlib.suppress(MiddlewareTermination):
                        await asyncio.wait_for(
                            agent.run(_FIRST_USER_MESSAGE, session=AgentSession()), timeout=args.wall_timeout
                        )
                    end_reason = "submit" if submit_state.summary is not None else "step_cap"
                except TimeoutError:
                    end_reason = "wall_timeout"
    except Exception as e:
        logger.exception("Rollout infrastructure failure")
        error_msg = repr(e)

    summary = RunSummary(
        model=args.model,
        skill_on=skill_on,
        end_reason=end_reason,
        wall_seconds=round(time.monotonic() - t_start, 2),
        submit_summary=submit_state.summary,
        error=error_msg,
    )
    summary_path.write_text(summary.model_dump_json(indent=2))
    logger.info("Rollout finished. Output dir: %s", out_dir)
    logger.info("Summary: %s", summary.model_dump_json())
    return 0 if end_reason != "error" else 1


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
    sys.exit(asyncio.run(_async_main(args)))


if __name__ == "__main__":
    main()
