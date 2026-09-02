"""Codex app-server driving steps, retaining native JSON-RPC semantics."""

from __future__ import annotations

import json
from typing import Any

from x.agentplane.native.codex import driver
from x.agentplane.native.process import NativeProcess


def launch_handshake(
    process: NativeProcess, *, cwd: str, model: str, effort: str, persist: bool = False
) -> dict[str, Any]:
    initialize = driver.initialize("capture-1")
    process.write(initialize)
    init_response = process.await_frame(lambda item: item.get("id") == "capture-1", timeout=30)
    process.write(driver.initialized())
    start = driver.thread_start("capture-2", cwd=cwd, model=model, effort=effort, persist=persist)
    process.write(start)
    started = process.await_frame(lambda item: item.get("id") == "capture-2", timeout=30)
    return {"initialize_response": init_response, "thread_start_response": started, "thread_id": _thread_id(started)}


def resume_handshake(process: NativeProcess, *, thread_id: str) -> dict[str, Any]:
    """Initialize a fresh app-server process and load its on-disk thread."""
    process.write(driver.initialize("capture-4"))
    init_response = process.await_frame(lambda item: item.get("id") == "capture-4", timeout=30)
    process.write(driver.initialized())
    resume = driver.thread_resume("capture-5", thread_id=thread_id)
    process.write(resume)
    resumed = process.await_frame(lambda item: item.get("id") == "capture-5", timeout=30)
    return {"initialize_response": init_response, "thread_resume_response": resumed, "thread_id": thread_id}


def _thread_id(thread_start_response: dict[str, Any]) -> str:
    result = thread_start_response.get("result")
    thread = result.get("thread") if isinstance(result, dict) else None
    thread_id_value = thread.get("id") if isinstance(thread, dict) else None
    if not isinstance(thread_id_value, str):
        raise ValueError("Codex thread/start did not return a durable thread id")
    return thread_id_value


def start_turn(process: NativeProcess, *, thread_id: str, request_id: str, text: str) -> str:
    """Send turn/start and return the turn id from its native acknowledgement."""
    process.write(driver.turn_start(request_id, thread_id=thread_id, text=text))
    started = process.await_frame(lambda item: item.get("id") == request_id, timeout=30)
    turn_result = started.get("result")
    turn = turn_result.get("turn") if isinstance(turn_result, dict) else None
    turn_id = turn.get("id") if isinstance(turn, dict) else None
    if not isinstance(turn_id, str):
        raise ValueError(f"Codex turn/start did not return a turn id: {started}")
    return turn_id


def await_turn_started(process: NativeProcess, *, timeout_s: float = 30) -> dict[str, Any]:
    return process.await_frame(lambda item: item.get("method") == "turn/started", timeout=timeout_s)


def await_turn_completed(process: NativeProcess, *, timeout_s: float = 120) -> dict[str, Any]:
    return process.await_frame(
        lambda item: item.get("method") == "turn/completed" and isinstance(item.get("params"), dict), timeout=timeout_s
    )


def submit(process: NativeProcess, *, thread_id: str, request_id: str, text: str) -> dict[str, Any]:
    turn_id = start_turn(process, thread_id=thread_id, request_id=request_id, text=text)
    return {"thread_id": thread_id, "turn_id": turn_id, "terminal": await_turn_completed(process)}


def steer(process: NativeProcess, *, thread_id: str, turn_id: str, request_id: str, text: str) -> dict[str, Any]:
    process.write(driver.steer(request_id, thread_id=thread_id, turn_id=turn_id, text=text))
    return process.await_frame(lambda item: item.get("id") == request_id, timeout=30)


def interrupt(process: NativeProcess, *, thread_id: str, turn_id: str, request_id: str) -> dict[str, Any]:
    process.write(driver.interrupt(request_id, thread_id=thread_id, turn_id=turn_id))
    return process.await_frame(lambda item: item.get("id") == request_id, timeout=30)


WAIT_PROMPT = (
    'Use shell to run `sh -c \'printf "wait_started\\n"; sleep 20; printf "wait_finished\\n"\'`; do not answer early.'
)


def submit_while_active(process: NativeProcess, *, thread_id: str, scenario: str) -> dict[str, Any]:
    turn_id = start_turn(process, thread_id=thread_id, request_id="capture-3", text=WAIT_PROMPT)
    active = await_turn_started(process)
    if scenario == "steering":
        response = steer(
            process,
            thread_id=thread_id,
            turn_id=turn_id,
            request_id="capture-4",
            text="Reply ONLY STEERED after the current tool action.",
        )
    else:
        process.write(
            driver.turn_start(
                "capture-4", thread_id=thread_id, text="Reply ONLY SECOND_INPUT_OBSERVED after current work."
            )
        )
        response = process.await_frame(lambda item: item.get("id") == "capture-4", timeout=30)
    return {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "active_evidence": active,
        "followup_response": response,
        "terminal": await_turn_completed(process),
    }


def interrupt_active_turn(process: NativeProcess, *, thread_id: str, with_queued_input: bool) -> dict[str, Any]:
    turn_id = start_turn(process, thread_id=thread_id, request_id="capture-3", text=WAIT_PROMPT)
    active = await_turn_started(process)
    queued_response = None
    if with_queued_input:
        process.write(driver.turn_start("capture-4", thread_id=thread_id, text="Queued input: reply only if admitted."))
        queued_response = process.await_frame(lambda item: item.get("id") == "capture-4", timeout=30)
    response = interrupt(process, thread_id=thread_id, turn_id=turn_id, request_id="capture-5")
    return {
        "thread_id": thread_id,
        "turn_id": turn_id,
        "active_evidence": active,
        "queued_response": queued_response,
        "interrupt_response": response,
        "terminal": await_turn_completed(process),
    }


# Bounded upstream retries keep connection-loss scenarios short.
MAX_RETRIES = 2


def config(*, endpoint: str) -> dict[str, Any]:
    """Native app-server configuration; a direct setup, not a Haku adapter."""
    return {
        # The environment supplies OPENAI_API_KEY; the provider keeps the Responses endpoint explicit.
        "model_provider": "agentplane",
        "model_providers": {
            "agentplane": {
                "name": "Agentplane LiteLLM",
                "base_url": endpoint,
                "env_key": "OPENAI_API_KEY",
                "wire_api": "responses",
                # Transport failures retry only through the stream loop, whose attempts are
                # visible as native `error` notices; a request-level retry budget would
                # multiply the upstream attempts silently.
                "request_max_retries": 0,
                "stream_max_retries": MAX_RETRIES,
            }
        },
        # Run deterministic tool probes without an interactive approval round-trip.
        "approval_policy": "never",
        "sandbox_mode": "danger-full-access",
        # Remove automatic skill discovery and its <skills_instructions> prompt block.
        "skills": {"bundled": {"enabled": False}, "include_instructions": False},
        # A fixed approval policy and no app, collaboration, or environment-context features:
        # omit their prompt blocks rather than recording unrelated harness guidance.
        "include_permissions_instructions": False,
        "include_apps_instructions": False,
        "include_collaboration_mode_instructions": False,
        "include_environment_context": False,
        "web_search": "disabled",
        "features": {
            # The scenarios use only shell execution; these tools are prompt bulk otherwise.
            "multi_agent": False,
            "view_image": False,
            # Retries are bounded by the provider's *_max_retries above.
            "unbounded_connection_retries": False,
        },
    }


def _toml_literal(value: object) -> str:
    # JSON literals for strings, booleans, integers, and arrays of those are valid TOML values.
    return json.dumps(value)


def _overrides(config: dict[str, Any], prefix: str = "") -> list[str]:
    """`-c dotted.key=value` pairs; app-server parses each value as TOML."""
    result: list[str] = []
    for key, value in config.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            result.extend(_overrides(value, f"{path}."))
        else:
            result.extend(["-c", f"{path}={_toml_literal(value)}"])
    return result


def command(binary: str, *, endpoint: str) -> list[str]:
    return [binary, *_overrides(config(endpoint=endpoint)), "app-server", "--listen", "stdio://"]


def environment(*, endpoint: str, token: str, codex_home: str) -> dict[str, str]:
    return {"CODEX_HOME": codex_home, "OPENAI_API_KEY": token, "OPENAI_BASE_URL": endpoint}
