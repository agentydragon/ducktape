"""Failure-path checks for command admission diagnostics."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Never, cast

import pytest
import pytest_bazel

from agentplane.protocol import command_pb2, event_log_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.client import RunnerClient
from agentplane.runner.testing.diagnostics import (
    AdmissionTrace,
    best_effort_write_admission_diagnostics,
    native_journal_evidence,
    write_admission_diagnostics,
)
from agentplane.runner.testing.fixtures import RunnerHandle
from util.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf


async def test_uncertain_admission_artifact_keeps_context_without_content(request: pytest.FixtureRequest) -> None:
    secret_marker = "command-body-and-token-must-not-be-copied"
    error = TimeoutError(secret_marker)
    trace = AdmissionTrace()
    trace.phase = "seed_noop_journal"
    trace.progress = "130/130"

    async def timeout() -> Never:
        raise error

    with pytest.raises(TimeoutError) as raised:
        await trace.command(
            "notifications",
            command_pb2.Command(
                command_id="admission-timeout", submit_input=command_pb2.SubmitInput(text=secret_marker)
            ),
            after_cursor=260,
            send=timeout,
        )

    assert raised.value is error
    artifact = best_effort_write_admission_diagnostics(
        undeclared_outputs_dir(),
        nodeid=request.node.nodeid,
        attempts=trace.attempts,
        client_responses=trace.client_responses,
        service_logs=[],
        native_sessions=[],
        capture_errors=[],
        trigger="injected_deadline",
    )
    assert artifact is not None
    data = json.loads(artifact.read_text())
    attempt = data["attempts"][0]
    assert attempt["error_type"] == "TimeoutError"
    assert attempt["mutation_outcome"] == "uncertain"
    assert attempt["result"] == "raised"
    assert attempt["command_id"] == "admission-timeout"
    assert attempt["operation"] == "submit_input"
    assert attempt["after_cursor"] == 260
    assert attempt["phase"] == "seed_noop_journal"
    assert attempt["progress"] == "130/130"
    assert secret_marker not in artifact.read_text()


async def test_capture_failure_does_not_replace_original_admission_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    error = TimeoutError("original admission timeout")
    trace = AdmissionTrace()

    async def timeout() -> Never:
        raise error

    blocked_output = tmp_path / "output-is-a-file"
    blocked_output.write_text("block directory creation")
    raised_error: TimeoutError | None = None
    try:
        await trace.command(
            "notifications",
            command_pb2.Command(
                command_id="admission-timeout", interrupt_turn=command_pb2.InterruptTurn(turn_id="fixture-turn")
            ),
            after_cursor=260,
            send=timeout,
        )
    except TimeoutError as caught:
        raised_error = caught
        best_effort_write_admission_diagnostics(
            blocked_output,
            nodeid="test_capture_failure_does_not_replace_original_admission_error",
            attempts=trace.attempts,
            client_responses=trace.client_responses,
            service_logs=[],
            native_sessions=[],
            capture_errors=[],
            trigger="injected_deadline",
        )

    assert raised_error is error
    assert "admission diagnostics capture failed: NotADirectoryError" in caplog.text


async def test_native_journal_snapshot_keeps_admission_and_terminal_metadata_only(
    client: RunnerClient, runner: RunnerHandle, spec: protocol_pb2.SessionSpec, request: pytest.FixtureRequest
) -> None:
    session_id = "admission-diagnostic-evidence"
    private_payload = "private-turn-payload-marker"
    command = command_pb2.Command(
        command_id="diagnostic-noop", interrupt_turn=command_pb2.InterruptTurn(turn_id=private_payload)
    )
    trace = AdmissionTrace()
    trace.phase = "runner_command_admission"
    trace.progress = "1/1"
    attached = await client.attach(session_id, spec=spec)
    try:

        async def submit() -> event_log_pb2.EventEntry:
            await attached.command(command)
            return await attached.until(
                lambda entry: (
                    entry.event.HasField("command_admitted")
                    and entry.event.command_admitted.command.command_id == command.command_id
                )
            )

        admitted = await trace.command(session_id, command, after_cursor=attached.cursor, send=submit)
        terminal = await attached.until(
            lambda entry: (
                entry.event.HasField("command_noop") and entry.event.command_noop.command_id == command.command_id
            )
        )
    finally:
        attached.cancel()

    snapshots = await native_journal_evidence(runner.runner)
    (snapshot,) = [item for item in snapshots if item["session_id"] == session_id]
    recorded_events = cast(list[dict[str, object]], snapshot["events"])
    admission, noop = [item for item in recorded_events if item.get("command_id") == command.command_id]
    assert (admission["kind"], admission["operation"], admission["cursor"]) == (
        "command_admitted",
        "interrupt_turn",
        admitted.cursor,
    )
    assert (noop["kind"], noop["cursor"]) == ("command_noop", terminal.cursor)
    assert cast(int, snapshot["last_cursor"]) >= terminal.cursor
    assert snapshot["harness_running"] is True
    assert snapshot["setup_state"] == "SETUP_STATE_NOT_REQUIRED"
    assert snapshot["truncated"] is False
    assert private_payload not in json.dumps(snapshot)
    bounded_snapshot = await native_journal_evidence(runner.runner, limit=1)
    (bounded_session,) = [item for item in bounded_snapshot if item["session_id"] == session_id]
    bounded_events = cast(list[dict[str, object]], bounded_session["events"])
    assert len(bounded_events) <= 1
    assert bounded_session["truncated"] is True
    artifact = write_admission_diagnostics(
        undeclared_outputs_dir(),
        nodeid=request.node.nodeid,
        attempts=trace.attempts,
        client_responses=[],
        service_logs=[],
        native_sessions=[snapshot],
        capture_errors=[],
        trigger="injected_live_command",
    )
    serialized = artifact.read_text()
    assert private_payload not in serialized
    artifact_data = json.loads(serialized)
    artifact_events = cast(list[dict[str, object]], artifact_data["native_sessions"][0]["events"])
    assert {event["cursor"] for event in artifact_events} >= {admitted.cursor, terminal.cursor}


if __name__ == "__main__":
    pytest_bazel.main()
