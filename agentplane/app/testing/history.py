"""Script fixture Sessions through History; observe their archive through its public API."""

from uuid import NAMESPACE_URL, UUID, uuid5

import grpc

from agentplane.grpc.calls import ServiceError
from agentplane.history_service import history_service_pb2
from agentplane.history_service.client import HistoryServiceClient
from agentplane.history_service.testing.history import History
from agentplane.proto import event_log_pb2

# gazelle:include_dep @pypi//protobuf
# gazelle:include_dep //agentplane/history_service:history_service_pb2

SANDBOX_NAMESPACE = "test-history-sandbox-namespace"


def fixture_session_id(sandbox: str, session_id: str) -> UUID:
    try:
        return UUID(session_id)
    except ValueError:
        return uuid5(NAMESPACE_URL, sandbox + "/" + session_id)


class SessionHistory:
    def __init__(self, history: History, *, sandbox_namespaces: dict[str, str] | None = None) -> None:
        self.history = history
        self.reader: HistoryServiceClient = history.service.reader
        self.sandbox_namespaces = sandbox_namespaces or {}

    async def seed(self, sandbox: str, session_id: str) -> UUID:
        public_id = fixture_session_id(sandbox, session_id)
        await self.history.open(public_id, sandbox_name=sandbox, sandbox_namespace=self.sandbox_namespaces.get(sandbox))
        return public_id

    async def events(self, thread_id: UUID, *, after_cursor: int = 0, limit: int) -> list[event_log_pb2.EventEntry]:
        entries: list[event_log_pb2.EventEntry] = []
        while len(entries) < limit:
            page = await self.reader.read_session_events(
                str(thread_id), after_cursor=after_cursor, limit=min(1000, limit - len(entries))
            )
            entries.extend(page.entries)
            if not page.entries or page.entries[-1].cursor >= page.last_cursor:
                break
            after_cursor = page.entries[-1].cursor
        return entries

    async def last_cursor(self, thread_id: UUID) -> int:
        return (await self.reader.get_session_summary(str(thread_id))).folded_through

    async def read_watermark(self, thread_id: UUID) -> int:
        return (await self.reader.get_session_summary(str(thread_id))).committed_cursor

    async def session_summary(self, thread_id: UUID) -> history_service_pb2.SessionSummary | None:
        try:
            return await self.reader.get_session_summary(str(thread_id))
        except ServiceError as error:
            if error.code != grpc.StatusCode.NOT_FOUND:
                raise
            return None
