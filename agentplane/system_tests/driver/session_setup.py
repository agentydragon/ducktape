"""Run a Session's setup through the public API and collect its retained output."""

import asyncio
from uuid import UUID

from google.protobuf.json_format import MessageToDict

from agentplane.app.testing.client import Client
from agentplane.app.testing.history_reads import SessionEventReader
from agentplane.proto import session_pb2
from agentplane.runner_gateway.dispatcher.testing_client import SessionDispatcherClient
from agentplane.system_tests.driver.tracing import tracer

BOUND_SECONDS = 300


async def run_setup(
    client: Client,
    dispatcher: SessionDispatcherClient,
    *,
    sandbox_name: str,
    sandbox_uid: str,
    session_id: UUID,
    harness: session_pb2.Harness,
    model: str,
    cwd: str,
    setup_script: str,
) -> tuple[str, str]:
    spec = session_pb2.SessionSpec(
        placement=session_pb2.SessionPlacement(
            environment_hosted=session_pb2.EnvironmentHostedPlacement(
                environment=session_pb2.ExactEnvironment(name=sandbox_name, uid=sandbox_uid), harness=harness
            )
        ),
        cwd=cwd,
        model=model,
        reasoning_effort="low",
    )
    with tracer.start_as_current_span(
        "system_test.session_setup.admission",
        attributes={"session.id": str(session_id)},
        record_exception=False,
        set_status_on_exception=False,
    ):
        await client.create_session_and_wait(
            sandbox_name,
            session_id,
            dispatcher=dispatcher,
            sandbox_uid=sandbox_uid,
            overrides=MessageToDict(spec),
            setup_script=setup_script,
        )
        thread_id = session_id
        # Runner admission precedes History's Thread projection, which owns the event stream.
        await client.wait_for_thread_projection(thread_id, timeout_s=BOUND_SECONDS)

    with tracer.start_as_current_span(
        "system_test.session_setup.output_wait",
        attributes={"session.id": str(session_id)},
        record_exception=False,
        set_status_on_exception=False,
    ):
        reader = SessionEventReader(client, thread_id, after_cursor=0)
        async with asyncio.timeout(BOUND_SECONDS):
            while True:
                await reader.read_page()
                terminal = next(
                    (
                        entry
                        for entry in reader.entries
                        if entry.event.HasField("setup_finished") or entry.event.HasField("setup_interrupted")
                    ),
                    None,
                )
                if terminal is not None:
                    stdout, stderr = bytearray(), bytearray()
                    for entry in reader.entries:
                        if entry.cursor > terminal.cursor:
                            break
                        if entry.event.HasField("setup_output"):
                            stdout.extend(entry.event.setup_output.stdout)
                            stderr.extend(entry.event.setup_output.stderr)
                    event = terminal.event
                    if event.HasField("setup_interrupted"):
                        raise AssertionError(
                            f"setup for Session {session_id} was interrupted: {stderr.decode(errors='replace')}"
                        )
                    assert event.setup_finished.exit_code == 0, (
                        f"setup for Session {session_id} exited {event.setup_finished.exit_code}: "
                        f"{stderr.decode(errors='replace')}\n{stdout.decode(errors='replace')}"
                    )
                    return stdout.decode(errors="replace"), stderr.decode(errors="replace")
                await reader.wait_for_next_page()
        raise AssertionError(f"setup for Session {session_id} emitted no finished event")


# gazelle:include_dep @pypi//protobuf
