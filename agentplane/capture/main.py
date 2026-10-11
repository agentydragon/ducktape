"""Record small, direct Claude/Codex protocol examples against a real model endpoint.

This is deliberately a discovery script: it writes raw JSONL pipes and LiteLLM bodies,
not a general runner, fixture framework, or provider-neutral API.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from agentplane.capture import codex_hook
from agentplane.capture.llm_recording_proxy import recording_proxy
from agentplane.capture.records import (
    CaptureMetadata,
    ConnectionDroppedRecord,
    ProxyErrorRecord,
    RequestRecord,
    ResponseChunkRecord,
)
from agentplane.native.async_process import write_jsonl
from agentplane.native.claude import scenarios as claude, wire as claude_wire
from agentplane.native.claude.async_run import ClaudeRun
from agentplane.native.codex import scenarios as codex
from agentplane.native.codex.async_run import CodexRun

SCENARIOS = (
    # keep-sorted start
    "baseline",
    "connection_exhaustion",
    "connection_retry",
    "file_edits",
    "hooks",
    "hooks_deny",
    "idle_resume",
    "interrupt",
    "launch",
    "post_exhaustion_follow_up",
    "post_failure_follow_up",
    "second_input",
    "shell",
    "steering",
    # keep-sorted end
)
# Hooks registered on every event in the scenarios' HOOK_EVENTS; the shell prompt makes PreToolUse
# fire, answered allow or, in `hooks_deny`, deny with a reason.
_HOOK_SCENARIOS = {"hooks": "allow", "hooks_deny": "deny"}

# The first loss is mid-stream; subsequent response-header losses also catch Claude's
# non-streaming fallback. Both harnesses run with a bounded retry budget (MAX_RETRIES in
# their scenarios modules), so a handful of losses exhausts it.
_FAULT_PLANS = {
    "connection_retry": ("message_start",),
    "post_failure_follow_up": ("text_delta",),
    "connection_exhaustion": ("message_start", *("response_headers",) * 8),
    "post_exhaustion_follow_up": ("message_start", *("response_headers",) * 8),
}

# A deterministic shell wait that keeps a turn active while the scenario steers, queues, or
# interrupts it.
_WAIT_COMMAND = 'sh -c \'printf "wait_started\\n"; sleep 20; printf "wait_finished\\n"\''
_CLAUDE_WAIT_THEN_REPLY = f"Use the Bash tool to run `{_WAIT_COMMAND}`; after it finishes reply ONLY WAIT_DONE."
_CLAUDE_WAIT = f"Use the Bash tool to run `{_WAIT_COMMAND}`; do not answer early."
_CODEX_WAIT = f"Use shell to run `{_WAIT_COMMAND}`; do not answer early."
_FOLLOW_UPS = {
    "post_failure_follow_up": "Reply with exactly: POST_FAILURE_FOLLOW_UP_OK",
    "post_exhaustion_follow_up": "Reply with exactly: POST_EXHAUSTION_FOLLOW_UP_OK",
}


def _fault_plan(provider: str, scenario: str) -> tuple[str, ...]:
    if provider == "claude":
        return _FAULT_PLANS.get(scenario, ())
    codex_events = {"message_start": "response.created", "text_delta": "response.output_text.delta"}
    return tuple(codex_events.get(event, event) for event in _FAULT_PLANS.get(scenario, ()))


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--provider", choices=("claude", "codex"), required=True)
    result.add_argument("--scenario", choices=SCENARIOS, required=True)
    result.add_argument("--binary", required=True)
    result.add_argument("--model", required=True)
    result.add_argument("--endpoint", required=True)
    result.add_argument("--credential-file", type=Path, required=True)
    result.add_argument("--workspace", type=Path, required=True)
    result.add_argument("--output", type=Path, required=True)
    return result


def _key(path: Path) -> str:
    if path.stat().st_mode & 0o077:
        raise ValueError("credential file must be 0600")
    return path.read_text().strip()


def _prepare(workspace: Path) -> None:
    workspace.mkdir(mode=0o700, parents=True, exist_ok=False)
    # Both native harnesses require their explicitly isolated state roots to exist.
    (workspace / ".claude").mkdir(mode=0o700)
    (workspace / ".codex").mkdir(mode=0o700)
    (workspace / "editable.txt").write_text("before\n")


@contextmanager
def _serve[Server: ThreadingHTTPServer](server: Server) -> Iterator[Server]:
    """Run a loopback HTTP server on a daemon thread for the lifetime of the block."""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _prompt(scenario: str) -> str:
    return {
        "baseline": "Reply with exactly: CAPTURE_BASELINE_OK",
        "shell": (
            "Use shell to run `printf 'PROBE_STDOUT\\n'` and "
            '`sh -c \'printf "probe stdout before failure\\n"; '
            'printf "probe stderr before failure\\n" >&2; exit 23\'`; report outcomes.'
        ),
        "file_edits": "Read editable.txt, change it to exactly `after\\n`, reread it, then reply FILE_EDIT_DONE.",
        "connection_retry": "Reply with exactly: CONNECTION_RETRY_OK",
        "connection_exhaustion": "Reply with exactly: CONNECTION_EXHAUSTION_OK",
        "post_failure_follow_up": "Reply with exactly: POST_FAILURE_FIRST_OK",
        "post_exhaustion_follow_up": "Reply with exactly: POST_EXHAUSTION_FIRST_OK",
        "hooks": "Use shell to run `printf 'HOOKS_PROBE_STDOUT\\n'`; report its output.",
        "hooks_deny": (
            "Use shell to run `printf 'HOOKS_PROBE_STDOUT\\n'`. If the tool call is refused, do not retry; "
            "reply with the refusal's reason verbatim."
        ),
    }[scenario]


def _turn_timeout_s(scenario: str) -> float:
    return 600 if "exhaustion" in scenario else 120


async def run(args: argparse.Namespace) -> None:
    if args.output.exists():
        raise ValueError("output must not exist")
    _prepare(args.workspace)
    args.output.mkdir(mode=0o700)
    for name in ("stdin.jsonl", "stdout.jsonl", "stderr.jsonl", "llm-requests.jsonl", "llm-responses.jsonl"):
        (args.output / name).touch(mode=0o600)
    hook_decision = _HOOK_SCENARIOS.get(args.scenario)
    if hook_decision is not None:
        (args.output / "hooks.jsonl").touch(mode=0o600)
    key = _key(args.credential_file)

    def record(event: RequestRecord | ResponseChunkRecord | ConnectionDroppedRecord | ProxyErrorRecord) -> None:
        write_jsonl(
            args.output / ("llm-requests.jsonl" if isinstance(event, RequestRecord) else "llm-responses.jsonl"), event
        )

    proxy = recording_proxy(
        upstream=args.endpoint, record=record, disconnect_after_events=_fault_plan(args.provider, args.scenario)
    )
    origin = f"http://127.0.0.1:{proxy.server_port}"
    with _serve(proxy):
        if args.provider == "claude":
            await _capture_claude(args, endpoint=origin, key=key, hook_decision=hook_decision)
        else:
            await _capture_codex(
                args, endpoint=origin + urlsplit(args.endpoint).path.rstrip("/"), key=key, hook_decision=hook_decision
            )
        response_rows = [json.loads(line) for line in (args.output / "llm-responses.jsonl").read_text().splitlines()]
        dropped = next((row for row in response_rows if row["kind"] == "connection_dropped"), None)
        if args.scenario in _FAULT_PLANS and dropped is None:
            raise ValueError("connection capture never reached its configured stream boundary")
        if args.scenario == "connection_retry":
            assert dropped is not None
            request_rows = [json.loads(line) for line in (args.output / "llm-requests.jsonl").read_text().splitlines()]
            dropped_number = int(dropped["capture_request_id"].removeprefix("llm-"))
            if len(request_rows) <= dropped_number:
                raise ValueError("connection-retry capture did not observe a native retry request")
        (args.output / "metadata.json").write_text(
            CaptureMetadata(provider=args.provider, scenario=args.scenario, model=args.model).model_dump_json() + "\n"
        )


async def _capture_claude(args: argparse.Namespace, *, endpoint: str, key: str, hook_decision: str | None) -> None:
    environment = {
        **os.environ,
        **claude.environment(endpoint=endpoint, token=key, config_dir=str(args.workspace / ".claude")),
    }

    def start(*, resume_id: str | None = None) -> ClaudeRun:
        return ClaudeRun(
            args.output,
            claude.command(args.binary, model=args.model, resume_id=resume_id, hooks=hook_decision is not None),
            cwd=args.workspace,
            environment=environment,
            responder=None if hook_decision is None else claude.hook_answers(deny_tools=hook_decision == "deny"),
            hooks=hook_decision is not None,
        )

    async with start() as session:
        resume_id = await _drive_claude(args, session)
    if args.scenario == "idle_resume":
        assert resume_id is not None
        async with start(resume_id=resume_id) as session:
            await _claude_turn(session, "Reply with exactly: IDLE_RESUME_OK")


async def _claude_turn(session: ClaudeRun, prompt: str, *, timeout_s: float = 120) -> claude_wire.ResultFrame:
    """Send one user frame and wait for the turn's terminal frame."""
    turn = await session.send(prompt)
    return await asyncio.wait_for(turn.result(), timeout=timeout_s)


async def _drive_claude(args: argparse.Namespace, session: ClaudeRun) -> str | None:
    """Run the scenario's first process; returns the session id an `idle_resume` needs."""
    if args.scenario == "launch":
        return None
    if args.scenario in {"steering", "second_input"}:
        # Claude has no separate steering frame: ordinary and mid-turn input share one native shape.
        first = await session.send(_CLAUDE_WAIT_THEN_REPLY)
        await asyncio.wait_for(first.active(), timeout=60)
        await session.send("Reply ONLY SECOND_INPUT_OBSERVED after your current work.")
        await asyncio.wait_for(first.result(), timeout=120)
        return None
    if args.scenario == "interrupt":
        first = await session.send(_CLAUDE_WAIT)
        await asyncio.wait_for(first.active(), timeout=60)
        await session.interrupt(cancel_queued=False)
        await asyncio.wait_for(first.result(), timeout=120)
        return None
    if args.scenario == "idle_resume":
        return (await _claude_turn(session, "Reply with exactly: IDLE_RESUME_SEED_OK")).session_id
    await _claude_turn(session, _prompt(args.scenario), timeout_s=_turn_timeout_s(args.scenario))
    if (follow_up := _FOLLOW_UPS.get(args.scenario)) is not None:
        await _claude_turn(session, follow_up)
    return None


async def _capture_codex(args: argparse.Namespace, *, endpoint: str, key: str, hook_decision: str | None) -> None:
    environment = {
        **os.environ,
        **codex.environment(endpoint=endpoint, token=key, codex_home=str(args.workspace / ".codex")),
    }

    def start(*, resume_thread_id: str | None = None) -> CodexRun:
        return CodexRun(
            args.output,
            codex.command(args.binary, endpoint=endpoint),
            cwd=args.workspace,
            environment=environment,
            thread_cwd=str(args.workspace),
            model=args.model,
            effort="low",
            persist=args.scenario == "idle_resume",
            config=None
            if hook_decision is None
            else codex.hooks_config(
                shlex.join([sys.executable, codex_hook.__file__, str(args.output / "hooks.jsonl"), hook_decision])
            ),
            resume_thread_id=resume_thread_id,
        )

    async with start() as session:
        await _drive_codex(args, session)
        thread_id = session.thread_id
    if args.scenario == "idle_resume":
        async with start(resume_thread_id=thread_id) as session:
            await _codex_turn(session, "Reply with exactly: IDLE_RESUME_OK")


async def _codex_turn(session: CodexRun, text: str, *, timeout_s: float = 120) -> None:
    turn = await session.start_turn(text)
    await asyncio.wait_for(turn.completed(), timeout=timeout_s)


async def _drive_codex(args: argparse.Namespace, session: CodexRun) -> None:
    if args.scenario == "launch":
        return
    if args.scenario in {"steering", "second_input", "interrupt"}:
        turn = await session.start_turn(_CODEX_WAIT)
        await asyncio.wait_for(turn.started(), timeout=30)
        if args.scenario == "steering":
            await session.steer(turn, "Reply ONLY STEERED after the current tool action.")
        elif args.scenario == "second_input":
            # A second turn/start during an active turn joins it as input.
            await session.start_turn("Reply ONLY SECOND_INPUT_OBSERVED after current work.")
        else:
            await session.interrupt(turn)
        await asyncio.wait_for(turn.completed(), timeout=120)
        return
    if args.scenario == "idle_resume":
        await _codex_turn(session, "Reply with exactly: IDLE_RESUME_SEED_OK")
        return
    await _codex_turn(session, _prompt(args.scenario), timeout_s=_turn_timeout_s(args.scenario))
    if (follow_up := _FOLLOW_UPS.get(args.scenario)) is not None:
        await _codex_turn(session, follow_up)


def main() -> None:
    asyncio.run(run(parser().parse_args()))


if __name__ == "__main__":
    main()
