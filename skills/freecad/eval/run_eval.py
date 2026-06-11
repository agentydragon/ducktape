"""Run a FreeCAD skill eval: agent produces baseplate.FCStd, transcript logged.

Usage:
  ANTHROPIC_API_KEY=sk-... bazel run //skills/freecad/eval:run_eval -- /tmp/eval-output
  ANTHROPIC_API_KEY=sk-... bazel run //skills/freecad/eval:run_eval -- /tmp/out --model claude-opus-4-6
"""

import argparse
import asyncio
import dataclasses
import json
import logging
import shutil
import tarfile
import time
from pathlib import Path

from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, ResultMessage, SystemMessage, UserMessage, query

from mcp_infra.exec.docker.types import AlwaysSetTo, BindMount, ContainerExecServerConfig
from util.bazel.runfiles import get_required_path
from util.oci import OciImage, load_oci_image

logger = logging.getLogger(__name__)

FREECAD_TEST = OciImage("_main/skills/freecad/eval/freecad_test.rloc", "freecad-test:pinned")
TASK_MD = "_main/skills/freecad/eval/baseplate/TASK.md"
DOCKER_LAUNCHER = "_main/mcp_infra/exec/docker_launcher"
SKILL_TAR = "_main/skills/freecad/freecad_tar.tar"

CONTAINER_WORKSPACE = Path("/workspace")
CONTAINER_SKILL_DIR = Path("/skill")

SYSTEM_PROMPT = f"""\
You are working inside a FreeCAD Docker container. You have exactly two tools:

1. `mcp__freecad__exec` — run shell commands inside the container
2. `mcp__freecad__read_image` — view PNG/JPEG/GIF/WebP images you produce

You have NO other tools. No Bash, Read, Write, Edit, Glob, Grep, or Skill tools.
Use `mcp__freecad__exec` for everything: reading files (`cat`), writing files
(`cat > file.py << 'EOF'`), running scripts, listing directories, etc.

The FreeCAD skill (SKILL.md) and all example scripts are at {CONTAINER_SKILL_DIR}/.
Start by running: `cat {CONTAINER_SKILL_DIR}/SKILL.md`

Your working directory is {CONTAINER_WORKSPACE}. Save all output files there.

FreeCAD is available as `freecadcmd`. Use `xvfb-run -a -s "-screen 0 1024x768x24" freecadcmd`
for any script that needs GUI/TechDraw rendering.
"""


async def run(output_dir: Path, model: str) -> None:
    logger.info("Loading FreeCAD Docker image")
    tag = load_oci_image(FREECAD_TEST)
    logger.info("Image loaded: %s", tag)

    workspace = output_dir / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)

    # Extract skill tarball into a host directory for bind-mounting.
    # Uses the tar (not the raw filegroup) to get exactly the skill package
    # contents without test/eval files that share the same runfiles subtree.
    skill_tar_path = get_required_path(SKILL_TAR)
    skill_host_dir = output_dir / "skill"
    if skill_host_dir.exists():
        shutil.rmtree(skill_host_dir)
    skill_host_dir.mkdir(parents=True)
    with tarfile.open(skill_tar_path) as tf:
        tf.extractall(skill_host_dir, filter="data")
    logger.info("Skill files staged at %s", skill_host_dir)

    # The tar extracts under a "freecad/" prefix (package_dir in skill_package).
    skill_content_dir = skill_host_dir / "freecad"
    skill_md = (skill_content_dir / "SKILL.md").read_text()
    task_text = get_required_path(TASK_MD).read_text()
    user_prompt = f"{skill_md}\n\n---\n\n{task_text}"

    launcher_binary = str(get_required_path(DOCKER_LAUNCHER))
    config_json = ContainerExecServerConfig(
        image=tag,
        working_dir=CONTAINER_WORKSPACE,
        binds=[
            BindMount(host_path=workspace, container_path=CONTAINER_WORKSPACE),
            BindMount(host_path=skill_content_dir, container_path=CONTAINER_SKILL_DIR, mode="ro"),
        ],
        allow_user_field=False,
        allow_env_field=False,
        cwd_policy=AlwaysSetTo(value=CONTAINER_WORKSPACE),
    ).model_dump_json()

    transcript_path = output_dir / "transcript.jsonl"
    messages: list[dict] = []
    start = time.monotonic()
    session_id: str | None = None

    with transcript_path.open("a") as log_f:
        async for message in query(
            prompt=user_prompt,
            options=ClaudeAgentOptions(
                cwd=workspace,
                disallowed_tools=[
                    "Agent",
                    "Bash",
                    "Edit",
                    "Glob",
                    "Grep",
                    "LSP",
                    "NotebookEdit",
                    "Read",
                    "Skill",
                    "WebFetch",
                    "WebSearch",
                    "Write",
                    "Task",
                    "TodoWrite",
                    "ToolSearch",
                ],
                allowed_tools=["mcp__freecad__*"],
                mcp_servers={"freecad": {"command": launcher_binary, "args": ["--config", config_json]}},
                permission_mode="bypassPermissions",
                max_turns=200,
                model=model,
                system_prompt=SYSTEM_PROMPT,
            ),
        ):
            entry = dataclasses.asdict(message)
            entry["_type"] = type(message).__name__
            entry["_timestamp"] = time.time()
            messages.append(entry)

            if isinstance(message, SystemMessage) and message.subtype == "init":
                session_id = message.data.get("session_id")
                logger.info("Session: %s", session_id)
            elif isinstance(message, AssistantMessage):
                if message.error:
                    logger.warning("Assistant error: %s", message.error)
                for block in message.content:
                    if hasattr(block, "text"):
                        preview = block.text[:200] + "..." if len(block.text) > 200 else block.text
                        logger.info("Assistant: %s", preview)
                    elif hasattr(block, "name"):
                        cmd = getattr(block, "input", {}).get("cmd", "")
                        if isinstance(cmd, list):
                            cmd = " ".join(cmd)
                        cmd_preview = (cmd[:150] + "...") if len(cmd) > 150 else cmd
                        logger.info("Tool call: %s(%s)", block.name, cmd_preview)
            elif isinstance(message, UserMessage):
                if isinstance(message.content, list):
                    for block in message.content:
                        # Skip image content blocks — just note their presence.
                        if isinstance(block, dict) and block.get("type") == "image":
                            src = block.get("source", {})
                            logger.info(
                                "Tool result: [image %s, %d bytes encoded]",
                                src.get("media_type", "?"),
                                len(src.get("data", "")),
                            )
                            continue
                        text = (
                            block.get("content", "")
                            if isinstance(block, dict)
                            else getattr(block, "content", str(block))
                        )
                        is_err = block.get("is_error") if isinstance(block, dict) else getattr(block, "is_error", None)
                        preview = (text[:300] + "...") if len(text) > 300 else text
                        if is_err:
                            logger.warning("Tool error: %s", preview)
                        elif preview:
                            logger.info("Tool result: %s", preview)
                elif isinstance(message.content, str) and message.content:
                    preview = (message.content[:300] + "...") if len(message.content) > 300 else message.content
                    logger.info("User: %s", preview)
            elif isinstance(message, ResultMessage):
                logger.info("Result: stop_reason=%s cost=$%.4f", message.stop_reason, message.total_cost_usd or 0)
            else:
                logger.info("Message: %s", type(message).__name__)

            log_f.write(json.dumps(entry, default=str) + "\n")
            log_f.flush()

    duration = time.monotonic() - start
    total_cost = sum(float(m.get("total_cost_usd") or 0) for m in messages if m["_type"] == "ResultMessage")
    turn_count = sum(1 for m in messages if m["_type"] == "AssistantMessage")

    metadata = {
        "model": model,
        "cost_usd": total_cost,
        "duration_s": round(duration, 1),
        "turns": turn_count,
        "session_id": session_id,
        "task": "baseplate",
    }
    (output_dir / "metadata.json").write_text(json.dumps(metadata, indent=2))

    artifacts = sorted(str(p.relative_to(workspace)) for p in workspace.rglob("*") if p.is_file())
    logger.info("Done. %d turns, $%.4f, %.0fs", turn_count, total_cost, duration)
    logger.info("Artifacts: %s", artifacts)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Run FreeCAD skill evaluation")
    parser.add_argument("output_dir", type=Path, help="Directory for eval outputs")
    parser.add_argument(
        "--model", default="claude-sonnet-4-6", help="Model ID (e.g. claude-sonnet-4-6, claude-opus-4-6)"
    )
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(run(output_dir, args.model))


if __name__ == "__main__":
    main()
