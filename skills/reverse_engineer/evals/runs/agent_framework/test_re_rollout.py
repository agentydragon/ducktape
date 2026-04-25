"""Single-rollout reverse-engineering eval against go_crypto_server.

Hands a Haiku agent the garbled go_crypto_server binary plus the
reverse_engineer skill, gives it shell access via MCP exec inside a stock
python:3.13-slim container, runs for up to 10 minutes, and writes the
transcript + the agent's recovered/ workspace to undeclared test outputs.

No assertions about the agent's output — this is a manual-inspection
harness. The test fails only on infrastructure errors (container won't
boot, model API call fails, etc.).
"""

import json
import logging
import os
import tarfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest
import pytest_bazel
from agent_framework import ChatResponse, Content, FunctionTool, Message
from agent_framework.anthropic import AnthropicClient
from fastmcp.client import Client
from mcp.types import TextContent
from pydantic import BaseModel, Field

from mcp_infra.exec.docker.types import BindMount
from skills.eval_infra.docker_exec import scratch_exec_server
from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

logger = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"
_MAX_STEPS = 200
_WALL_TIMEOUT_SECONDS = 600  # 10 min agent budget; Bazel test timeout is "long" (900s).

_TARGET_BINARY_RLOCATION = "_main/skills/reverse_engineer/evals/specimens/go_crypto_server/go_crypto_server_garbled.bin"
_SKILL_TAR_RLOCATION = "_main/skills/reverse_engineer/reverse_engineer_tar.tar"


# -- Transcript record types --


class ToolCallRecord(BaseModel):
    name: str
    args: dict[str, Any]
    result_text: str
    duration_ms: int


class TranscriptEntry(BaseModel):
    step: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    response_text: str = ""
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


class RunSummary(BaseModel):
    model: str
    skill_on: bool
    end_reason: Literal["submit", "step_cap", "wall_timeout", "error"]
    steps: int
    wall_seconds: float
    submit_summary: str | None = None
    error: str | None = None


# -- Skill loading --


def _load_skill(extract_dir: Path) -> tuple[Path, str]:
    """Extract the skill tar; return (host dir to bind-mount, SKILL.md text)."""
    tar_path = get_required_path(_SKILL_TAR_RLOCATION)
    with tarfile.open(tar_path) as tf:
        tf.extractall(extract_dir)
    skill_dir = extract_dir / "reverse_engineer"
    skill_md = (skill_dir / "SKILL.md").read_text()
    return skill_dir, skill_md


# -- Prompts --


def _build_system_prompt(*, skill_on: bool, skill_md_text: str | None) -> str:
    base = (
        "You are reverse-engineering a stripped Go binary located at /work/target.\n"
        "Recover its source as Go files under /work/recovered/ (your working directory).\n"
        "You have shell access via the `exec` tool — `cmd` is a list of strings, no shell\n"
        "expansion. Stdout and stderr are returned together. Install whatever you need\n"
        "(apt-get install -y ..., pip install ..., curl ...). The container has internet.\n"
        "When you have gone as far as you can, call `submit` with a one-paragraph summary\n"
        "of what the program does and which parts you are confident vs unsure about."
    )
    if not skill_on:
        return base
    assert skill_md_text is not None, "skill_md_text required when skill_on=True"
    return (
        f"{base}\n\n"
        "A reverse-engineering skill is available. Its SKILL.md is included below verbatim.\n"
        "The files SKILL.md references (e.g. examples/pclntool.go, examples/garble_re_recipe.sh)\n"
        "live inside the container at /work/.skill/. You can read them with `cat /work/.skill/...`,\n"
        "run scripts with `bash /work/.skill/examples/garble_re_recipe.sh ...`, build Go helpers\n"
        "with `go run /work/.skill/examples/pclntool.go ...`, etc.\n\n"
        "--- BEGIN SKILL.md ---\n"
        f"{skill_md_text}\n"
        "--- END SKILL.md ---\n"
    )


_FIRST_USER_MESSAGE = (
    "Reverse-engineer the binary at /work/target. Recover its source as Go files under "
    "/work/recovered/. You have a 10-minute wall-clock budget and at most 200 turns. "
    "When done, call `submit` with a one-paragraph summary."
)


# -- Tool bridges --


def _make_exec_tool(mcp_client: Client) -> FunctionTool:
    """Bridge the MCP exec tool into an agent_framework FunctionTool."""

    async def exec(cmd: list[str], timeout_ms: int = 60000) -> str:
        """Run a command in the scratch container. cmd is a list of strings (no shell)."""
        result = await mcp_client.call_tool("exec", {"cmd": cmd, "timeout_ms": timeout_ms})
        return "\n".join(block.text for block in result.content if isinstance(block, TextContent))

    return FunctionTool(
        name="exec",
        description="Run a command in the scratch container. cmd is a list of strings (no shell).",
        func=exec,
    )


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


# -- Rollout loop --


def _extract_function_calls(response: ChatResponse) -> list[Content]:
    msg = response.messages[0]
    return [c for c in msg.contents if c.type == "function_call"]


def _extract_response_text(response: ChatResponse) -> str:
    return response.messages[0].text or ""


async def _run_rollout(
    *,
    model_client: AnthropicClient,
    system_prompt: str,
    user_message: str,
    tools: list[FunctionTool],
    submit_state: _SubmitState,
    transcript_path: Path,
) -> tuple[Literal["submit", "step_cap", "wall_timeout", "error"], int, str | None]:
    tool_map = {t.name: t for t in tools}
    history: list[Message] = [Message("system", [system_prompt]), Message("user", [user_message])]

    deadline = time.monotonic() + _WALL_TIMEOUT_SECONDS

    with transcript_path.open("w") as transcript_f:
        for step in range(1, _MAX_STEPS + 1):
            if submit_state.summary is not None:
                return "submit", step - 1, submit_state.summary
            if time.monotonic() > deadline:
                return "wall_timeout", step - 1, None

            try:
                response = await model_client.get_response(
                    history, options={"tools": tools, "tool_choice": "required", "allow_multiple_tool_calls": False}
                )
            except Exception as e:
                logger.exception("Model call failed at step %d", step)
                entry = TranscriptEntry(step=step, response_text=f"<model error: {e}>")
                transcript_f.write(entry.model_dump_json() + "\n")
                transcript_f.flush()
                return "error", step - 1, None

            history.append(response.messages[0])
            response_text = _extract_response_text(response)
            function_calls = _extract_function_calls(response)

            entry = TranscriptEntry(step=step, response_text=response_text)
            tool_results: list[Content] = []

            for fc in function_calls:
                assert fc.name is not None, f"function_call missing name: {fc}"
                assert fc.call_id is not None, f"function_call missing call_id: {fc}"
                args = json.loads(fc.arguments) if isinstance(fc.arguments, str) else (fc.arguments or {})
                tool = tool_map.get(fc.name)
                t0 = time.monotonic()
                if tool is None:
                    result_text = f"Error: unknown tool {fc.name!r}"
                else:
                    try:
                        out = await tool.invoke(arguments=args)
                        result_text = out[0].text if out and out[0].text else ""
                    except Exception as e:
                        result_text = f"Error: {e}"
                duration_ms = int((time.monotonic() - t0) * 1000)
                entry.tool_calls.append(
                    ToolCallRecord(name=fc.name, args=args, result_text=result_text, duration_ms=duration_ms)
                )
                tool_results.append(Content.from_function_result(fc.call_id, result=result_text))

            transcript_f.write(entry.model_dump_json() + "\n")
            transcript_f.flush()

            if tool_results:
                history.append(Message("tool", tool_results))

        return "step_cap", _MAX_STEPS, None


# -- Test entry point --


@pytest.mark.parametrize("skill_on", [True], ids=["skill_on"])
async def test_re_rollout(skill_on: bool) -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        pytest.skip("ANTHROPIC_API_KEY not set")

    out_dir = undeclared_outputs_dir()
    suffix = "skill_on" if skill_on else "skill_off"
    workspace = out_dir / f"recovered_{suffix}"
    workspace.mkdir(parents=True, exist_ok=True)
    skill_extract = out_dir / f"skill_extract_{suffix}"
    transcript_path = out_dir / f"transcript_{suffix}.jsonl"
    summary_path = out_dir / f"summary_{suffix}.json"

    target_path = get_required_path(_TARGET_BINARY_RLOCATION)

    binds: list[BindMount] = [
        BindMount(host_path=target_path.resolve(), container_path=Path("/work/target"), mode="ro"),
        BindMount(host_path=workspace.resolve(), container_path=Path("/work/recovered"), mode="rw"),
    ]
    skill_md_text: str | None = None
    if skill_on:
        skill_extract.mkdir(parents=True, exist_ok=True)
        skill_dir, skill_md_text = _load_skill(skill_extract)
        binds.append(BindMount(host_path=skill_dir.resolve(), container_path=Path("/work/.skill"), mode="ro"))

    system_prompt = _build_system_prompt(skill_on=skill_on, skill_md_text=skill_md_text)

    submit_state = _SubmitState()
    submit_tool = _make_submit_tool(submit_state)

    model_client = AnthropicClient(model=_MODEL)
    t_start = time.monotonic()
    try:
        async with (
            scratch_exec_server(binds=binds, working_dir=Path("/work/recovered")) as server,
            Client(server) as mcp_client,
        ):
            tools = [_make_exec_tool(mcp_client), submit_tool]
            end_reason, steps, submit_summary = await _run_rollout(
                model_client=model_client,
                system_prompt=system_prompt,
                user_message=_FIRST_USER_MESSAGE,
                tools=tools,
                submit_state=submit_state,
                transcript_path=transcript_path,
            )
            error_msg = None
    except Exception as e:
        logger.exception("Rollout infrastructure failure")
        end_reason = "error"
        steps = 0
        submit_summary = None
        error_msg = repr(e)
    finally:
        if hasattr(model_client, "close"):
            await model_client.close()

    summary = RunSummary(
        model=_MODEL,
        skill_on=skill_on,
        end_reason=end_reason,
        steps=steps,
        wall_seconds=round(time.monotonic() - t_start, 2),
        submit_summary=submit_summary,
        error=error_msg,
    )
    summary_path.write_text(summary.model_dump_json(indent=2))
    logger.info("Rollout finished: %s", summary.model_dump_json())

    assert end_reason != "error", f"infrastructure failure: {error_msg}"


if __name__ == "__main__":
    pytest_bazel.main()
