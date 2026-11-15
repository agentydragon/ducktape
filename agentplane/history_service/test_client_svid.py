"""History client supports service-SVID calls without bearer metadata."""

from unittest.mock import AsyncMock, Mock

import grpc
import pytest
import pytest_bazel
from google.protobuf.empty_pb2 import Empty

from agentplane.history_service.api.history import history_service_pb2
from agentplane.history_service.client import HistoryServiceClient

# The generated service stub's dependency chain needs the protobuf runtime as a direct dependency.
# gazelle:include_dep @pypi//protobuf


class _Stream:
    def __init__(self) -> None:
        self._read = False
        self.cancelled = False

    async def read(self) -> object:
        if self._read:
            return grpc.aio.EOF
        self._read = True
        return history_service_pb2.WatchSessionSummariesResponse(reconnect_required=Empty())

    def cancel(self) -> None:
        self.cancelled = True


async def test_history_unary_and_watch_send_no_bearer_for_service_svid(monkeypatch: pytest.MonkeyPatch) -> None:
    unary = AsyncMock(return_value=history_service_pb2.SessionSummary())
    stream = _Stream()
    watch = Mock(return_value=stream)
    stub = type("Stub", (), {"GetSessionSummary": unary, "WatchSessionSummaries": watch})()
    monkeypatch.setattr(HistoryServiceClient, "stub", property(lambda _self: stub))
    client = HistoryServiceClient("unused", token_file=None, request_timeout_s=1)

    await client.get_session_summary("session")
    watcher = client.watch_session_summaries()
    with pytest.raises(StopAsyncIteration):
        await anext(watcher)

    unary_call = unary.await_args
    watch_call = watch.call_args
    assert unary_call is not None
    assert watch_call is not None
    assert unary_call.kwargs["metadata"] == ()
    assert watch_call.kwargs["metadata"] == ()
    assert stream.cancelled


if __name__ == "__main__":
    pytest_bazel.main()
