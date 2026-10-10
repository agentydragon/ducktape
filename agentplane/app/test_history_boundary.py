"""App history reads cross the real client and the public gRPC wire contract."""

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import grpc
import pytest
import pytest_bazel

from agentplane.app.testing.history_service import HistoryService
from agentplane.app.testing.thread_test_support import event_entry
from agentplane.history_service.client import HistoryServiceClient, HistoryServiceError
from agentplane.protocol import event_pb2

# gazelle:include_dep @pypi//protobuf


@pytest.fixture
def history_peer() -> HistoryService:
    return HistoryService()


@pytest.fixture
async def history_client(history_peer: HistoryService, tmp_path: Path) -> AsyncIterator[HistoryServiceClient]:
    async with history_peer.connect(tmp_path / "history-token") as client:
        yield client


async def test_history_peer_pages_original_evidence_over_grpc(
    history_peer: HistoryService, history_client: HistoryServiceClient
) -> None:
    session = uuid4()
    history_peer.open(session)
    entries = [event_entry(i, native=event_pb2.Native(line=f"evidence {i}")) for i in range(1, 4)]
    history_peer.publish(session, entries)
    entries[0].event.native.line = "caller mutated its protobuf"
    first = await history_client.read_session_events(str(session), limit=2)
    assert first.last_cursor == 3
    assert [e.cursor for e in first.entries] == [1, 2]
    assert first.entries[0].event.native.line == "evidence 1"
    tail = await history_client.read_session_events(str(session), after_cursor=2, limit=2)
    assert list(tail.entries) == entries[2:]
    assert history_peer.requests[-1].after_cursor == 2
    observations = await history_client.read_session_observations(str(session), before_cursor=3, limit=1)
    assert [(o.cursor, o.kind) for o in observations.observations] == [(2, "native")]


async def test_history_rpc_failure_is_not_an_empty_archive(
    history_peer: HistoryService, history_client: HistoryServiceClient
) -> None:
    history_peer.failure = grpc.StatusCode.PERMISSION_DENIED
    with pytest.raises(HistoryServiceError) as failure:
        await history_client.read_session_events(str(uuid4()))
    assert failure.value.code == grpc.StatusCode.PERMISSION_DENIED


if __name__ == "__main__":
    pytest_bazel.main()
