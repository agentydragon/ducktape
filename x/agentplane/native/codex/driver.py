"""Explicit native Codex app-server JSON-RPC frame constructors."""

from __future__ import annotations

from typing import Any

from x.agentplane.native.codex import wire

# This replaces Codex's broad default coding-agent policy while preserving the
# app-server's native tool behavior for each scenario.
BASE_INSTRUCTIONS = "You are a concise test assistant. Follow user requests using the available tools."


def initialize(request_id: str) -> wire.InitializeRequest:
    return wire.InitializeRequest(
        id=request_id,
        params=wire.InitializeParams(client_info=wire.ClientInfo(name="agentplane-capture", version="0.1")),
    )


def initialized() -> wire.InitializedNotification:
    return wire.InitializedNotification()


def thread_start(
    request_id: str,
    *,
    cwd: str,
    model: str,
    effort: str,
    persist: bool = False,
    config: dict[str, Any] | None = None,
    instructions: str = "",
) -> wire.ThreadStartRequest:
    """`config` adds per-thread `config.toml` keys; app-server layers them over its own configuration.

    `instructions` becomes the thread's developer instructions, which the app-server keeps with the
    thread, so a `thread/resume` does not restate them. Empty sends no key for them.
    """
    return wire.ThreadStartRequest(
        id=request_id,
        params=wire.ThreadStartParams(
            cwd=cwd,
            approval_policy="never",
            sandbox="danger-full-access",
            # Non-resume probes do not need a stored rollout, thread title, or session history.
            ephemeral=not persist,
            model=model,
            # Replaces the broad default coding-agent policy in recorded model requests.
            base_instructions=BASE_INSTRUCTIONS,
            developer_instructions=instructions or None,
            config={"model_reasoning_effort": effort, **(config or {})},
        ),
    )


def thread_resume(
    request_id: str, *, thread_id: str, base_instructions: str = "", instructions: str = ""
) -> wire.ThreadResumeRequest:
    """Load a persisted thread after a new app-server process starts.

    `base_instructions` and `instructions` are the resume's overrides for the thread's coding-agent
    policy and its developer instructions. Empty sends no key for either. The two do not behave
    alike, and `//x/agentplane/harness_tests/codex:test_instructions` pins the difference.
    """
    return wire.ThreadResumeRequest(
        id=request_id,
        params=wire.ThreadResumeParams(
            thread_id=thread_id,
            base_instructions=base_instructions or None,
            developer_instructions=instructions or None,
        ),
    )


def turn_start(request_id: str, *, thread_id: str, text: str) -> wire.TurnStartRequest:
    return wire.TurnStartRequest(
        id=request_id, params=wire.TurnStartParams(thread_id=thread_id, input=[wire.TextInput(text=text)])
    )


def steer(request_id: str, *, thread_id: str, turn_id: str, text: str) -> wire.TurnSteerRequest:
    return wire.TurnSteerRequest(
        id=request_id,
        params=wire.TurnSteerParams(thread_id=thread_id, expected_turn_id=turn_id, input=[wire.TextInput(text=text)]),
    )


def interrupt(request_id: str, *, thread_id: str, turn_id: str) -> wire.TurnInterruptRequest:
    return wire.TurnInterruptRequest(
        id=request_id, params=wire.TurnInterruptParams(thread_id=thread_id, turn_id=turn_id)
    )
