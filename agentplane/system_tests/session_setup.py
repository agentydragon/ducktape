"""Run a Session's setup through the public API and collect its retained output."""

import asyncio
from uuid import UUID

from google.protobuf.json_format import MessageToDict

from agentplane.app.testing.client import Client
from agentplane.proto import runner_pb2
from agentplane.system_tests.tracing import tracer

BOUND_SECONDS = 300


async def run_setup(
    client: Client,
    *,
    sandbox_name: str,
    sandbox_uid: UUID,
    session_id: UUID,
    harness: runner_pb2.Harness,
    model: str,
    cwd: str,
    setup_script: str,
) -> tuple[str, str]:
    spec = runner_pb2.SessionSpec(harness=harness, cwd=cwd, model=model, reasoning_effort="low")
    with tracer.start_as_current_span(
        "system_test.session_setup.admission",
        attributes={"session.id": str(session_id)},
        record_exception=False,
        set_status_on_exception=False,
    ):
        await client.create_session(
            sandbox_name, session_id, sandbox_uid=sandbox_uid, overrides=MessageToDict(spec), setup_script=setup_script
        )
        thread_id = session_id
        # Runner admission precedes History's Thread projection, which owns the event stream.
        await client.wait_for_thread_projection(thread_id, timeout_s=BOUND_SECONDS)

    stdout, stderr = bytearray(), bytearray()
    with tracer.start_as_current_span(
        "system_test.session_setup.output_wait",
        attributes={"session.id": str(session_id)},
        record_exception=False,
        set_status_on_exception=False,
    ):
        async with asyncio.timeout(BOUND_SECONDS):
            async for entry in client.events(thread_id, after=0, read_seconds=BOUND_SECONDS):
                event = entry.event
                if event.HasField("setup_output"):
                    stdout.extend(event.setup_output.stdout)
                    stderr.extend(event.setup_output.stderr)
                elif event.HasField("setup_interrupted"):
                    raise AssertionError(
                        f"setup for Session {session_id} was interrupted: {stderr.decode(errors='replace')}"
                    )
                elif event.HasField("setup_finished"):
                    assert event.setup_finished.exit_code == 0, (
                        f"setup for Session {session_id} exited {event.setup_finished.exit_code}: "
                        f"{stderr.decode(errors='replace')}\n{stdout.decode(errors='replace')}"
                    )
                    return stdout.decode(errors="replace"), stderr.decode(errors="replace")
        raise AssertionError(f"setup for Session {session_id} emitted no finished event")


# gazelle:include_dep @pypi//protobuf
