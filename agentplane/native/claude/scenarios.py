"""Claude Code stream-json launch configuration and hook answers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from agentplane.native.claude import driver, wire

SYSTEM_PROMPT = "You are a concise test assistant. Follow user requests using the available tools."
# A preset session title stops Claude's separate title-generating model call.
SESSION_NAME = "agentplane-capture"
TOOLS = ("Bash", "Edit", "Read")
# The events a hooks capture registers: the blocking events a single-agent turn without compaction
# reaches, plus the session brackets. Subagent and compaction events stay unregistered.
HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SessionEnd")
HOOK_DENIAL = "agentplane capture: this tool call is denied by a PreToolUse hook"


def hook_answers(*, deny_tools: bool) -> Callable[[dict[str, Any]], Awaitable[wire.ControlResponse | None]]:
    """A frame responder answering every hook callback as soon as it arrives and allowing tool
    permission prompts. A PreToolUse answer carries the decision the model sees: allow, or deny
    with `HOOK_DENIAL` as its reason; every other event gets `{}`, no opinion."""

    async def answer(frame: dict[str, Any]) -> wire.ControlResponse | None:
        match wire.parse_frame(frame):
            case wire.ControlRequestFrame(request_id=request_id, request=wire.HookCallback(input=hook_input)):
                output: dict[str, Any] = {}
                if hook_input.get("hook_event_name") == "PreToolUse":
                    output = {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "deny" if deny_tools else "allow",
                            "permissionDecisionReason": HOOK_DENIAL if deny_tools else "agentplane capture allows",
                        }
                    }
                return driver.hook_output(request_id, output)
            case wire.ControlRequestFrame(request_id=request_id, request=wire.CanUseTool(input=tool_input)):
                return driver.allow_tool(request_id, tool_input)
        return None

    return answer


def command(
    binary: str,
    *,
    model: str,
    resume_id: str | None = None,
    session_id: str | None = None,
    replay_user_messages: bool = False,
    effort: str | None = None,
    hooks: bool = False,
    slash_commands: bool = False,
    tools: tuple[str, ...] = TOOLS,
) -> list[str]:
    """`session_id` fixes a fresh session's id; with `resume_id` the resumed session keeps its own.

    `replay_user_messages` makes the harness echo each user frame back with `isReplay`, and it also
    emits a `command_lifecycle` frame (queued, started, completed) per user frame uuid.

    `hooks` keeps hooks enabled, so the ones a client registers at initialize fire, and has the harness
    report each firing as `hook_started`/`hook_response` system frames.
    """
    if resume_id and session_id:
        raise ValueError("a resumed session keeps its id; pass resume_id or session_id, not both")
    result = [
        binary,
        # --safe-mode blocks plugins and hooks from adding their prompt/tool bulk; it also disables the
        # hooks a client registers at initialize. --setting-sources= below still keeps plugins out.
        *(["--include-hook-events"] if hooks else ["--safe-mode"]),
        # This suppresses the optional prompt_suggestion frame after each turn.
        "--prompt-suggestions=false",
        # An empty source list ignores user, project, and local settings files.
        "--setting-sources=",
        # With no --mcp-config, this also excludes MCP servers configured on disk.
        "--strict-mcp-config",
        # Replaces Claude Code's default system prompt; the scenarios need no coding-agent policy.
        "--system-prompt",
        SYSTEM_PROMPT,
        "--name",
        SESSION_NAME,
        # Retain only the tools the scenarios exercise, not Claude's full tool catalog.
        "--tools",
        ",".join(tools),
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-prompt-tool",
        "stdio",
        "--include-partial-messages",
        "--input-format",
        "stream-json",
        "--model",
        model,
    ]
    # Most captures omit the skills/slash catalog; the compaction probe needs /compact.
    if not slash_commands:
        result.append("--disable-slash-commands")
    if resume_id:
        result.extend(["--resume", resume_id])
    if session_id:
        result.extend(["--session-id", session_id])
    if replay_user_messages:
        result.append("--replay-user-messages")
    if effort:
        result.extend(["--effort", effort])
    return result


# Bounded upstream retries keep connection-loss scenarios short.
MAX_RETRIES = 2


def environment(*, endpoint: str, token: str, config_dir: str) -> dict[str, str]:
    return {
        "ANTHROPIC_AUTH_TOKEN": token,
        "ANTHROPIC_BASE_URL": endpoint,
        "CLAUDE_CONFIG_DIR": config_dir,
        "CLAUDE_CODE_MAX_RETRIES": str(MAX_RETRIES),
        # No telemetry, update checks, or other traffic beside the model endpoint.
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    }
