"""Explicit native Codex app-server JSON-RPC frame constructors."""

from __future__ import annotations

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
    request_id: str, *, cwd: str, model: str, effort: str, persist: bool = False
) -> wire.ThreadStartRequest:
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
            config={"model_reasoning_effort": effort},
        ),
    )


def thread_resume(request_id: str, *, thread_id: str) -> wire.ThreadResumeRequest:
    """Load a persisted thread after a new app-server process starts."""
    return wire.ThreadResumeRequest(id=request_id, params=wire.ThreadResumeParams(thread_id=thread_id))


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
