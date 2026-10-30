"""Setup admission, executor effects, and durable one-shot recovery over the public RPCs."""

import asyncio
import os
import shlex
from pathlib import Path
from textwrap import dedent
from typing import Any, override

import grpc
import pytest
import pytest_bazel

from agentplane.native.codex.testing.responses_sse import Message, response_stream
from agentplane.proto import event_pb2, runner_pb2
from agentplane.runner import runner_service_pb2
from agentplane.x.central_sampler import sampler_pb2, sampler_pb2_grpc
from agentplane.x.central_sampler.testing import METADATA, Endpoint, Request, open_request, service, settings
from util.testing.undeclared_outputs import undeclared_outputs_dir


class SetupEndpoint(Endpoint):
    def __init__(self, completion: Path):
        super().__init__()
        self.completion = completion
        self.requests = 0

    @override
    def _parse_request(self, value: Any) -> Request:
        self.requests += 1
        assert self.completion.is_file(), "provider contacted before setup completed"
        return super()._parse_request(value)


async def through_event(stub: sampler_pb2_grpc.SamplerAsyncStub, session_id: str, kind: str) -> list[event_pb2.Event]:
    stream = stub.FollowEvents(sampler_pb2.FollowEventsRequest(session_id=session_id, follow=True), metadata=METADATA)
    events = []
    try:
        async with asyncio.timeout(15):
            async for frame in stream:
                events.append(frame.entry.event)
                if frame.entry.event.HasField(kind):
                    return events
    finally:
        stream.cancel()
    raise AssertionError(f"event stream ended before {kind}")


async def recorded_events(stub: sampler_pb2_grpc.SamplerAsyncStub, session_id: str) -> list[event_pb2.Event]:
    return [
        frame.entry.event
        async for frame in stub.FollowEvents(
            sampler_pb2.FollowEventsRequest(session_id=session_id), metadata=METADATA, timeout=10
        )
    ]


async def test_setup_runs_at_open_and_gates_queued_sampling(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    workspace = tmp_path / "new-workspace"
    gate = tmp_path / "release"
    os.mkfifo(gate)
    async with SetupEndpoint(workspace / "complete") as endpoint:
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        request = open_request(workspace)
        request.setup_script = dedent(f"""\
            printf x >> attempts
            printf 'setup stdout\\n'
            cat {shlex.quote(str(gate))} >/dev/null
            printf 'setup stderr\\n' >&2
            pwd > setup-cwd
            printf done > complete
            """)
        async with service(config) as stub:
            opened = await stub.OpenSession(request, metadata=METADATA, timeout=10)
            assert opened.attached.setup_state == runner_pb2.SETUP_STATE_RUNNING
            await through_event(stub, request.session_id, "setup_output")
            duplicate = await stub.OpenSession(request, metadata=METADATA, timeout=10)
            assert duplicate.attached.setup_state == runner_pb2.SETUP_STATE_RUNNING
            command = runner_service_pb2.SubmitInputRequest(
                session_id=request.session_id, command_id="queued-during-setup", text="Read the prepared workspace."
            )
            admission = await stub.SubmitInput(command, metadata=METADATA, timeout=10)
            before = await recorded_events(stub, request.session_id)
            assert not any(event.HasField("harness_started") for event in before)
            assert endpoint.requests == 0
            # Opening a FIFO writer is the release signal; no timing assumption decides ordering.
            await asyncio.wait_for(asyncio.to_thread(gate.write_text, "continue\n"), timeout=10)
            async with await endpoint.next() as exchange:
                assert (workspace / "setup-cwd").read_text() == str(workspace) + "\n"
                assert exchange.request.body["input"][-1]["content"] == command.text
                await exchange.send(*response_stream([Message("Setup is ready.")], model="test-model").events)
            events = await through_event(stub, request.session_id, "turn_completed")
            assert events[-1].turn_completed.status == event_pb2.TURN_STATUS_COMPLETED
            stdout = b"".join(event.setup_output.stdout for event in events if event.HasField("setup_output"))
            stderr = b"".join(event.setup_output.stderr for event in events if event.HasField("setup_output"))
            assert stdout == b"setup stdout\n"
            assert stderr == b"setup stderr\n"
            finished = next(index for index, event in enumerate(events) if event.HasField("setup_finished"))
            started = next(index for index, event in enumerate(events) if event.HasField("harness_started"))
            assert events[finished].setup_finished.exit_code == 0
            assert finished < started
            assert sum(event.HasField("setup_started") for event in events) == 1
            assert await stub.SubmitInput(command, metadata=METADATA, timeout=10) == admission
        async with service(config) as stub:
            reopened = await stub.OpenSession(request, metadata=METADATA, timeout=10)
            assert reopened.attached.setup_state == runner_pb2.SETUP_STATE_SUCCEEDED
            events = await recorded_events(stub, request.session_id)
            assert sum(event.HasField("setup_started") for event in events) == 1
            assert (workspace / "attempts").read_text() == "x"
            changed = sampler_pb2.OpenSessionRequest()
            changed.CopyFrom(request)
            changed.setup_script += "\nprintf changed\n"
            with pytest.raises(grpc.aio.AioRpcError) as conflict:
                await stub.OpenSession(changed, metadata=METADATA, timeout=10)
            assert conflict.value.code() == grpc.StatusCode.ALREADY_EXISTS


@pytest.mark.parametrize("failure", ["exit 23", "false", "printf '%s' \"$CENTRAL_SETUP_UNSET_TEST_VARIABLE\""])
async def test_failed_setup_remains_terminal_after_reopen(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path, failure: str
) -> None:
    async with SetupEndpoint(tmp_path / "unreachable") as endpoint:
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        request = open_request(tmp_path)
        request.setup_script = f"printf x >> attempts\n{failure}\nprintf done > unreachable\n"
        async with service(config) as stub:
            await stub.OpenSession(request, metadata=METADATA, timeout=10)
            events = await through_event(stub, request.session_id, "setup_finished")
            assert events[-1].setup_finished.exit_code != 0
            if failure == "exit 23":
                assert events[-1].setup_finished.exit_code == 23
        async with service(config) as stub:
            reopened = await stub.OpenSession(request, metadata=METADATA, timeout=10)
            assert reopened.attached.setup_state == runner_pb2.SETUP_STATE_FAILED
            snapshot = await stub.GetSession(
                sampler_pb2.GetSessionRequest(session_id=request.session_id), metadata=METADATA, timeout=10
            )
            assert snapshot.blocked
            with pytest.raises(grpc.aio.AioRpcError) as rejected:
                await stub.SubmitInput(
                    runner_service_pb2.SubmitInputRequest(
                        session_id=request.session_id, command_id="after-failed-setup", text="Must not sample."
                    ),
                    metadata=METADATA,
                    timeout=10,
                )
            assert rejected.value.code() == grpc.StatusCode.FAILED_PRECONDITION
            events = await recorded_events(stub, request.session_id)
            assert sum(event.HasField("setup_started") for event in events) == 1
            assert sum(event.HasField("setup_finished") for event in events) == 1
            assert not any(event.HasField("harness_started") for event in events)
            assert (tmp_path / "attempts").read_text() == "x"
            assert not (tmp_path / "unreachable").exists()
            assert endpoint.requests == 0


async def test_restart_during_setup_interrupts_without_replaying_effects(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    gate = tmp_path / "never-release"
    os.mkfifo(gate)
    async with SetupEndpoint(tmp_path / "unreachable") as endpoint:
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        request = open_request(tmp_path)
        request.setup_script = dedent(f"""\
            printf x >> attempts
            printf ready
            cat {shlex.quote(str(gate))} >/dev/null
            printf done > unreachable
            """)
        async with service(config) as stub:
            await stub.OpenSession(request, metadata=METADATA, timeout=10)
            await through_event(stub, request.session_id, "setup_output")
            command = runner_service_pb2.SubmitInputRequest(
                session_id=request.session_id, command_id="waiting-for-setup", text="Never sample this input."
            )
            receipt = await stub.SubmitInput(command, metadata=METADATA, timeout=10)
        async with service(config) as stub:
            await through_event(stub, request.session_id, "setup_interrupted")
            reopened = await stub.OpenSession(request, metadata=METADATA, timeout=10)
            assert reopened.attached.setup_state == runner_pb2.SETUP_STATE_INTERRUPTED
            snapshot = await stub.GetSession(
                sampler_pb2.GetSessionRequest(session_id=request.session_id), metadata=METADATA, timeout=10
            )
            assert snapshot.blocked
            assert await stub.SubmitInput(command, metadata=METADATA, timeout=10) == receipt
            events = await recorded_events(stub, request.session_id)
            assert sum(event.HasField("setup_started") for event in events) == 1
            assert sum(event.HasField("setup_interrupted") for event in events) == 1
            assert not any(event.HasField("setup_finished") or event.HasField("harness_started") for event in events)
            assert (tmp_path / "attempts").read_text() == "x"
            assert not (tmp_path / "unreachable").exists()
            assert endpoint.requests == 0


async def test_setup_script_limit_counts_utf8_bytes(
    database_url: str, execution_target: str, codex_binary: str, code_mode_host: str, tmp_path: Path
) -> None:
    async with Endpoint() as endpoint:
        config = settings(
            database_url, endpoint.origin, execution_target, codex_binary, code_mode_host, undeclared_outputs_dir()
        )
        request = open_request(tmp_path)
        request.setup_script = "#" + "λ" * 32768 + "\n"
        async with service(config) as stub:
            with pytest.raises(grpc.aio.AioRpcError) as rejected:
                await stub.OpenSession(request, metadata=METADATA, timeout=10)
            assert rejected.value.code() == grpc.StatusCode.INVALID_ARGUMENT
            request.setup_script = "#" + "λ" * 32767 + "\n"
            await stub.OpenSession(request, metadata=METADATA, timeout=10)
            events = await through_event(stub, request.session_id, "setup_finished")
            assert events[-1].setup_finished.exit_code == 0


if __name__ == "__main__":
    pytest_bazel.main()

# Generated protobuf stubs require their direct typing dependency.
# gazelle:include_dep @pypi//protobuf
