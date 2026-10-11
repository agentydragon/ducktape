"""Forward a command without starting a harness; the runner remains the admission authority."""

from agentplane.protocol import command_pb2, event_log_pb2
from agentplane.runner import protocol_pb2
from agentplane.runner.client import RunnerClient
from agentplane.runner.errors import RunnerError

# gazelle:include_dep @pypi//protobuf


async def admit_running_command(
    client: RunnerClient, session_id: str, command: command_pb2.Command, *, after_cursor: int, timeout_s: float
) -> event_log_pb2.EventEntry:
    """Return the runner's exact admission receipt, not transport or harness confirmation.

    The caller must select and authorize the runner/session before calling this transport helper.
    Open deliberately carries no spec: an unknown session fails, and a stopped harness stays stopped.
    ``after_cursor`` is a runner-log cursor; to reconcile an uncertain earlier submission, select a
    cursor preceding its possible admission and reuse the unchanged command (including its ID).

    ``timeout_s`` bounds the receipt wait, not connection setup or writes. Failure or cancellation
    does not prove rejection: the runner may already have committed the command. No retry, offline
    queue, or new command ID is introduced here. The caller retains ownership of the client.
    """
    attachment = await client.attach(session_id, after_cursor=after_cursor)
    try:
        if attachment.attached.harness_state != protocol_pb2.HARNESS_STATE_RUNNING:
            raise RunnerError("session is stopped; explicitly open it before sending commands")
        await attachment.command(command)
        await attachment.detach()
        # Writes only reach gRPC's outgoing buffer. Keep reading until the runner proves it
        # committed the entire Command, not merely another command with the same ID.
        return await attachment.until(
            lambda entry: entry.event.HasField("command_admitted") and entry.event.command_admitted.command == command,
            timeout_s=timeout_s,
        )
    finally:
        attachment.cancel()
