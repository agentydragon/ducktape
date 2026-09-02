"""Explicit native Codex app-server JSON-RPC frame constructors."""

from __future__ import annotations

from typing import Any

# This replaces Codex's broad default coding-agent policy while preserving the
# app-server's native tool behavior for each scenario.
BASE_INSTRUCTIONS = "You are a concise test assistant. Follow user requests using the available tools."


def initialize(request_id: str) -> dict[str, Any]:
    return {
        "method": "initialize",
        "id": request_id,
        "params": {"clientInfo": {"name": "agentplane-capture", "version": "0.1"}, "capabilities": None},
    }


def initialized() -> dict[str, Any]:
    return {"method": "initialized"}


def thread_start(request_id: str, *, cwd: str, model: str, effort: str, persist: bool = False) -> dict[str, Any]:
    return {
        "method": "thread/start",
        "id": request_id,
        "params": {
            "cwd": cwd,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            # Non-resume probes do not need a stored rollout, thread title, or session history.
            "ephemeral": not persist,
            "model": model,
            # Replaces the broad default coding-agent policy in recorded model requests.
            "baseInstructions": BASE_INSTRUCTIONS,
            "config": {"model_reasoning_effort": effort},
        },
    }


def thread_resume(request_id: str, *, thread_id: str) -> dict[str, Any]:
    """Load a persisted thread after a new app-server process starts."""
    return {"method": "thread/resume", "id": request_id, "params": {"threadId": thread_id}}


def turn_start(request_id: str, *, thread_id: str, text: str) -> dict[str, Any]:
    return {
        "method": "turn/start",
        "id": request_id,
        "params": {"threadId": thread_id, "input": [{"type": "text", "text": text, "text_elements": []}]},
    }


def steer(request_id: str, *, thread_id: str, turn_id: str, text: str) -> dict[str, Any]:
    return {
        "method": "turn/steer",
        "id": request_id,
        "params": {
            "threadId": thread_id,
            # The steer is rejected unless this names the currently active turn.
            "expectedTurnId": turn_id,
            "input": [{"type": "text", "text": text, "text_elements": []}],
        },
    }


def interrupt(request_id: str, *, thread_id: str, turn_id: str) -> dict[str, Any]:
    return {"method": "turn/interrupt", "id": request_id, "params": {"threadId": thread_id, "turnId": turn_id}}
