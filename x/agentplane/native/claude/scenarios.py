"""Claude Code stream-json driving steps, retaining its native control JSON."""

from __future__ import annotations

from typing import Any

from x.agentplane.native.claude import driver
from x.agentplane.native.process import NativeProcess

SYSTEM_PROMPT = "You are a concise test assistant. Follow user requests using the available tools."
# A preset session title stops Claude's separate title-generating model call.
SESSION_NAME = "agentplane-capture"
TOOLS = ("Bash", "Edit", "Read")


def launch_handshake(process: NativeProcess) -> dict[str, Any]:
    frame = driver.initialize()
    process.write(frame)
    request_id = frame["request_id"]
    reply = process.await_frame(
        lambda item: (
            item.get("type") == "control_response"
            and isinstance(item.get("response"), dict)
            and item["response"].get("request_id") == request_id
        ),
        timeout=30,
    )
    return {"initialize_request_id": request_id, "control_response": reply}


def send(process: NativeProcess, text: str) -> str:
    """Write one native user frame; returns its uuid."""
    frame = driver.user_frame(text)
    process.write(frame)
    return str(frame["uuid"])


def await_result(process: NativeProcess, *, timeout_s: float = 120) -> dict[str, Any]:
    return process.await_frame(lambda item: item.get("type") == "result", timeout=timeout_s)


def await_active(process: NativeProcess, *, timeout_s: float = 60) -> dict[str, Any]:
    """The first frame showing a turn in progress: a stream event or an assistant tool use."""
    return process.await_frame(
        lambda item: (
            item.get("type") == "stream_event"
            or (
                item.get("type") == "assistant"
                and any(block.get("type") == "tool_use" for block in item.get("message", {}).get("content", []))
            )
        ),
        timeout=timeout_s,
    )


def submit(process: NativeProcess, prompt: str, *, timeout_s: float = 120) -> dict[str, Any]:
    """Send one user frame and wait for the turn's terminal frame."""
    prompt_uuid = send(process, prompt)
    return {"prompt_uuid": prompt_uuid, "terminal": await_result(process, timeout_s=timeout_s)}


def session_id(result: dict[str, Any]) -> str:
    session_id_value = result.get("session_id")
    if not isinstance(session_id_value, str):
        raise ValueError("Claude result did not return a durable session id")
    return session_id_value


def interrupt(process: NativeProcess, *, cancel_queued: bool) -> dict[str, Any]:
    request = driver.interrupt(cancel_queued=cancel_queued)
    process.write(request)
    return process.await_frame(
        lambda item: (
            item.get("type") == "control_response"
            and item.get("response", {}).get("request_id") == request["request_id"]
        ),
        timeout=30,
    )


def submit_while_active(process: NativeProcess) -> dict[str, Any]:
    """Write a second user frame while a deterministic shell wait is active.

    Claude has no separate steering frame: ordinary and mid-turn input share one native shape.
    """
    first = send(
        process,
        'Use the Bash tool to run `sh -c \'printf "wait_started\\n"; sleep 20; '
        'printf "wait_finished\\n"\'`; after it finishes reply ONLY WAIT_DONE.',
    )
    active = await_active(process)
    second = send(process, "Reply ONLY SECOND_INPUT_OBSERVED after your current work.")
    return {"first_uuid": first, "second_uuid": second, "active_evidence": active, "terminal": await_result(process)}


def interrupt_active_turn(process: NativeProcess, *, with_queued_input: bool) -> dict[str, Any]:
    first = send(
        process,
        'Use the Bash tool to run `sh -c \'printf "wait_started\\n"; sleep 20; '
        'printf "wait_finished\\n"\'`; do not answer early.',
    )
    active = process.await_frame(lambda item: item.get("type") in {"stream_event", "assistant", "system"}, timeout=60)
    queued_uuid = None
    if with_queued_input:
        queued_uuid = send(process, "This is intentionally queued input; acknowledge only if admitted.")
    response = interrupt(process, cancel_queued=with_queued_input)
    return {
        "initial_uuid": first,
        "queued_uuid": queued_uuid,
        "active_evidence": active,
        "interrupt_response": response,
        "terminal": await_result(process),
    }


def command(
    binary: str,
    *,
    model: str,
    resume_id: str | None = None,
    session_id: str | None = None,
    replay_user_messages: bool = False,
    effort: str | None = None,
) -> list[str]:
    """`session_id` fixes a fresh session's id; with `resume_id` the resumed session keeps its own.

    `replay_user_messages` makes the harness echo each user frame back with `isReplay`, and it also
    emits a `command_lifecycle` frame (queued, started, completed) per user frame uuid.
    """
    if resume_id and session_id:
        raise ValueError("a resumed session keeps its id; pass resume_id or session_id, not both")
    result = [
        binary,
        # --safe-mode blocks plugins and hooks from adding their prompt/tool bulk.
        "--safe-mode",
        # This disables skills and slash commands, removing their catalog from the prompt.
        "--disable-slash-commands",
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
        ",".join(TOOLS),
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
