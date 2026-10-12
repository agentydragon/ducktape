"""The writer tests bind pushed Sessions to, and Sessions it has pushed."""

from uuid import UUID, uuid4

from agentplane.history_service.storage.store import Store, Writer
from agentplane.proto import event_log_pb2

# gazelle:include_dep @pypi//protobuf

WRITER = Writer(namespace="test-sandbox-namespace", name="test-sandbox-runner", uid="test-runner-account-uid")


async def retained_session(history: Store, entries: list[event_log_pb2.EventEntry]) -> UUID:
    """A new Session bound to `WRITER` whose committed history is `entries`."""
    session_id = uuid4()
    await history.bind_writer(session_id, WRITER)
    await history.append(session_id, entries)
    return session_id
