"""Composed Dispatcher retry after the real Runtime commits but its reply is lost."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shutil
from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import pytest_bazel
from google.protobuf.message import DecodeError
from python.runfiles import Runfiles
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.env.e2e.testing import events
from agentplane.env.e2e.testing.config import RunnerTestConfig
from agentplane.env.e2e.testing.process_fixtures import RunnerProcess
from agentplane.env.runtime.control import environment_runtime_pb2
from agentplane.history_service.testing.go_service import GoHistoryService
from agentplane.proto import event_log_pb2, session_pb2
from agentplane.runner_gateway.dispatcher.api.session import session_dispatcher_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient, accepted_session, admitted
from agentplane.sandboxes.testing.kubernetes import SandboxCluster
from agentplane.testing.scripted_model import ScriptedModel, Text
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir

# gazelle:include_dep @pypi//protobuf

_TEST_RUNTIME_BINARY = (
    "ducktape/agentplane/env/runtime/cmd/"
    "admission-reply-loss-test-environment-runtime_/admission-reply-loss-test-environment-runtime"
)
_FAULT_DIRECTORY = "admission-reply-loss-test"


@pytest.fixture
def runtime_binary() -> Path:
    """Use the fault-instrumented Runtime only for this test module."""
    return Runfiles.CreateOrRaise().root() / _TEST_RUNTIME_BINARY


@pytest.fixture(autouse=True)
def retain_attempt_records(config: RunnerTestConfig) -> Iterator[None]:
    """Keep only allowlisted request/response records, including when assertions fail."""
    yield
    source = config.state_dir / _FAULT_DIRECTORY
    if source.is_dir():
        records = sorted(source.glob("attempt-*.json"))
        if records:
            destination = undeclared_outputs_dir() / f"admission-reply-loss-{uuid4().hex}"
            destination.mkdir(parents=True, exist_ok=True)
            for record in records:
                shutil.copy2(record, destination / record.name)


async def _create_session(dispatcher: SessionDispatcherClient, spec: session_pb2.SessionSpec) -> str:
    session_id = str(uuid4())
    await dispatcher.unary(
        dispatcher.stub.CreateSession, session_dispatcher_pb2.CreateSessionRequest(session_id=session_id, spec=spec)
    )
    await accepted_session(dispatcher, UUID(session_id))
    return session_id


async def _read_attempts(directory: Path, count: int) -> list[dict[str, object]]:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(30), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            paths, records = await asyncio.to_thread(_load_attempts, directory)
            assert len(paths) >= count, f"Runtime recorded {len(paths)} matching handler results, wanted {count}"
            assert len(records) == count, f"unexpected extra matching Runtime calls: {paths}"
            return records
    raise AssertionError(f"Runtime did not record {count} matching handler results")


def _load_attempts(directory: Path) -> tuple[list[Path], list[dict[str, object]]]:
    paths = sorted(directory.glob("attempt-*.json"))
    return paths, [json.loads(path.read_text()) for path in paths]


async def _recorded_turn(history: GoHistoryService, session_id: str, after: int) -> list[event_log_pb2.EventEntry]:
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(30), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            entries = list(
                (await history.reader.read_session_events(session_id, after_cursor=after, limit=1000)).entries
            )
            assert any(events.turn_completed(entry) for entry in entries)
            return entries
    raise AssertionError("History did not commit turn completion")


def _response(record: dict[str, object]) -> environment_runtime_pb2.SubmitInputResponse:
    encoded = record.get("response")
    assert isinstance(encoded, str), record
    try:
        return environment_runtime_pb2.SubmitInputResponse.FromString(base64.b64decode(encoded, validate=True))
    except (ValueError, DecodeError) as error:
        raise AssertionError(f"invalid recorded Runtime response: {error}") from error


@pytest.mark.asyncio
async def test_dispatcher_retries_after_real_runtime_admission_reply_is_lost(
    dispatcher: SessionDispatcherClient,
    history_service: GoHistoryService,
    sandbox: SandboxCluster,
    config: RunnerTestConfig,
    start_runtime: Callable[[], Awaitable[RunnerProcess]],
    model: ScriptedModel,
    spec: session_pb2.SessionSpec,
) -> None:
    await start_runtime()
    session_id = await _create_session(dispatcher, spec)
    command_id = str(uuid4())
    fault_directory = config.state_dir / _FAULT_DIRECTORY
    await asyncio.to_thread(fault_directory.joinpath("command").write_text, command_id)

    # One caller submission causes two actual, authenticated Runtime handler calls. The first
    # handler commits and returns an admission before the test-only interceptor discards it.
    first_dispatch = await dispatcher.submit_input(
        session_id, command_id, sandbox.name, sandbox.uid, "Reply LOST-REPLY"
    )
    attempts = await _read_attempts(fault_directory, 2)
    assert len(attempts) == 2
    assert attempts[0]["request"] == attempts[1]["request"]
    responses = [_response(record) for record in attempts]
    assert responses[0] == responses[1]
    assert responses[0].HasField("admission")
    for index, record in enumerate(attempts):
        assert record["command_id"] == command_id, record
        assert record["handler_code"] == "OK", record
        assert record["returned_code"] == ("Unavailable" if index == 0 else "OK"), record
        encoded_request = record.get("request")
        assert isinstance(encoded_request, str), record
        request_bytes = base64.b64decode(encoded_request, validate=True)
        request = environment_runtime_pb2.SubmitInputRequest.FromString(request_bytes)
        assert request.command_id == command_id
        assert request.session_id == session_id
        assert request.text == "Reply LOST-REPLY"
        assert record["request_sha256"] == hashlib.sha256(request_bytes).hexdigest()
    assert responses[0].admission.event.command_admitted.command.command_id == command_id

    final_dispatch = await admitted(dispatcher, first_dispatch)
    assert final_dispatch.state in {
        session_dispatcher_pb2.DELIVERY_STATE_RUNNER_ADMITTED,
        session_dispatcher_pb2.DELIVERY_STATE_HARNESS_CONFIRMED,
    }
    assert final_dispatch.admission == responses[0].admission

    # This later repeat is the Dispatcher receipt-replay control. It cannot account for the two
    # Runtime handler calls above and must not trigger a third call.
    receipt_replay = await dispatcher.submit_input(
        session_id, command_id, sandbox.name, sandbox.uid, "Reply LOST-REPLY"
    )
    assert receipt_replay.command_id == final_dispatch.command_id
    assert receipt_replay.admission == final_dispatch.admission
    assert len((await asyncio.to_thread(_load_attempts, fault_directory))[0]) == 2

    first_model_request = await model.request()
    assert first_model_request.user_texts == ["Reply LOST-REPLY"]
    await model.reply(first_model_request, Text("RECOVERED ONCE"))
    await _recorded_turn(history_service, session_id, responses[0].admission.cursor - 1)

    next_id = str(uuid4())
    next_dispatch = await dispatcher.submit_input(session_id, next_id, sandbox.name, sandbox.uid, "Reply NEXT")
    next_admission = await admitted(dispatcher, next_dispatch)
    next_model_request = await model.request()
    assert next_model_request.user_texts == ["Reply LOST-REPLY", "Reply NEXT"]
    await model.reply(next_model_request, Text("NEXT COMPLETED"))
    await _recorded_turn(history_service, session_id, next_admission.admission.cursor - 1)

    entries = list((await history_service.reader.read_session_events(session_id, limit=1000)).entries)
    admitted_entries = events.of_kind(entries, "command_admitted")
    assert admitted_entries == [responses[0].admission, next_admission.admission]
    assert len(events.of_kind(entries, "turn_completed")) == 2
    assert model.request_count == 2
    events.assert_contiguous(entries)
    assert len((await asyncio.to_thread(_load_attempts, fault_directory))[0]) == 2


if __name__ == "__main__":
    pytest_bazel.main()
