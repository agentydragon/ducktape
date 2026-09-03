"""Record small, direct Claude/Codex protocol examples against a real model endpoint.

This is deliberately a discovery script: it writes raw JSONL pipes and LiteLLM bodies,
not a general runner, fixture framework, or provider-neutral API.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from urllib.parse import urlsplit

from x.agentplane.capture import codex_hook
from x.agentplane.capture.llm_recording_proxy import recording_proxy
from x.agentplane.capture.records import (
    CaptureMetadata,
    ConnectionDroppedRecord,
    ProxyErrorRecord,
    RequestRecord,
    ResponseChunkRecord,
)
from x.agentplane.native.claude import scenarios as claude
from x.agentplane.native.codex import scenarios as codex
from x.agentplane.native.process import NativeProcess, serve, write_jsonl

SCENARIOS = (
    "launch",
    "baseline",
    "shell",
    "file_edits",
    "steering",
    "second_input",
    "interrupt",
    "idle_resume",
    "connection_retry",
    "connection_exhaustion",
    "post_failure_follow_up",
    "post_exhaustion_follow_up",
    "hooks",
    "hooks_deny",
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


def run(args: argparse.Namespace) -> None:
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
    if args.provider == "claude":
        proxy_endpoint = origin
        environment = {
            **os.environ,
            **claude.environment(endpoint=origin, token=key, config_dir=str(args.workspace / ".claude")),
        }
    else:
        proxy_endpoint = origin + urlsplit(args.endpoint).path.rstrip("/")
        environment = {
            **os.environ,
            **codex.environment(endpoint=proxy_endpoint, token=key, codex_home=str(args.workspace / ".codex")),
        }

    def start_process(*, resume_id: str | None = None) -> NativeProcess:
        command = (
            claude.command(args.binary, model=args.model, resume_id=resume_id, hooks=hook_decision is not None)
            if args.provider == "claude"
            else codex.command(args.binary, endpoint=proxy_endpoint)
        )
        process = NativeProcess(args.output, command, cwd=args.workspace, environment=environment)
        if hook_decision is not None and args.provider == "claude":
            process.frame_handler = claude.hook_answers(deny_tools=hook_decision == "deny")
        return process

    with serve(proxy):
        with start_process() as process:
            if args.provider == "claude":
                claude.launch_handshake(process, hooks=hook_decision is not None)
                thread_id = None
            else:
                thread_id = codex.launch_handshake(
                    process,
                    cwd=str(args.workspace),
                    model=args.model,
                    effort="low",
                    persist=args.scenario == "idle_resume",
                    config=None
                    if hook_decision is None
                    else codex.hooks_config(
                        shlex.join(
                            [sys.executable, codex_hook.__file__, str(args.output / "hooks.jsonl"), hook_decision]
                        )
                    ),
                )["thread_id"]
            resume_id = _drive(args, process, thread_id)
        if args.scenario == "idle_resume":
            with start_process(resume_id=resume_id) as process:
                if args.provider == "claude":
                    claude.launch_handshake(process)
                    claude.submit(process, "Reply with exactly: IDLE_RESUME_OK")
                else:
                    assert thread_id is not None
                    codex.resume_handshake(process, thread_id=thread_id)
                    codex.submit(
                        process, thread_id=thread_id, request_id="capture-6", text="Reply with exactly: IDLE_RESUME_OK"
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


def _drive(args: argparse.Namespace, process: NativeProcess, thread_id: str | None) -> str | None:
    """Run the scenario's first process; returns the Claude session id an `idle_resume` needs."""
    if args.scenario == "launch":
        return None
    if args.scenario in {"steering", "second_input"}:
        if args.provider == "claude":
            claude.submit_while_active(process)
        else:
            assert thread_id is not None
            codex.submit_while_active(process, thread_id=thread_id, scenario=args.scenario)
        return None
    if args.scenario == "interrupt":
        if args.provider == "claude":
            claude.interrupt_active_turn(process, with_queued_input=False)
        else:
            assert thread_id is not None
            codex.interrupt_active_turn(process, thread_id=thread_id, with_queued_input=False)
        return None
    if args.scenario == "idle_resume":
        if args.provider == "claude":
            return claude.session_id(claude.submit(process, "Reply with exactly: IDLE_RESUME_SEED_OK")["terminal"])
        assert thread_id is not None
        codex.submit(
            process, thread_id=thread_id, request_id="capture-3", text="Reply with exactly: IDLE_RESUME_SEED_OK"
        )
        return None
    timeout_s = 600 if "exhaustion" in args.scenario else 120
    if args.provider == "claude":
        claude.submit(process, _prompt(args.scenario), timeout_s=timeout_s)
    else:
        assert thread_id is not None
        codex.submit(process, thread_id=thread_id, request_id="capture-3", text=_prompt(args.scenario))
    if args.scenario in {"post_failure_follow_up", "post_exhaustion_follow_up"}:
        follow_up = (
            "Reply with exactly: POST_FAILURE_FOLLOW_UP_OK"
            if args.scenario == "post_failure_follow_up"
            else "Reply with exactly: POST_EXHAUSTION_FOLLOW_UP_OK"
        )
        if args.provider == "claude":
            claude.submit(process, follow_up)
        else:
            assert thread_id is not None
            codex.submit(process, thread_id=thread_id, request_id="capture-4", text=follow_up)
    return None


def main() -> None:
    run(parser().parse_args())


if __name__ == "__main__":
    main()
