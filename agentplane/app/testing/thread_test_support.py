"""HTTP observations and mutations of real Go app Threads."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from google.protobuf.timestamp_pb2 import Timestamp
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app.testing.models import ThreadMetadata
from agentplane.proto import event_log_pb2, event_pb2, session_pb2

SPEC = session_pb2.SessionSpec(
    harness=session_pb2.HARNESS_CLAUDE, cwd="/state/work", model="test-model", reasoning_effort="low"
)


def event_entry(cursor: int, **observation: object) -> event_log_pb2.EventEntry:
    at = Timestamp()
    at.FromDatetime(datetime(2026, 9, 2, 12, 0, tzinfo=UTC) + timedelta(seconds=cursor))
    event = event_pb2.Event(at=at, **observation)  # type: ignore[arg-type]
    return event_log_pb2.EventEntry(
        cursor=cursor, origin=event_log_pb2.EventOrigin(source_id="test-runner", sequence=cursor), event=event
    )


class ThreadHTTP:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def wait(self, thread_id: UUID) -> None:
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(15), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                metadata = await self.http.get(f"/upstream/app/v1/threads/{thread_id}")
                assert metadata.status_code == 200, metadata.text
                summary = await self.http.get(f"/upstream/history/v1/sessions/{thread_id}/summary")
                assert summary.status_code == 200, summary.text

    async def rename(self, thread_id: UUID, name: str) -> None:
        await self.wait(thread_id)
        response = await self.http.patch(f"/threads/{thread_id}", json={"name": name})
        response.raise_for_status()

    async def archive(self, thread_id: UUID) -> None:
        response = await self.http.post(f"/threads/{thread_id}/archive")
        response.raise_for_status()

    async def list_threads(self, *, sandbox: str | None = None) -> list[ThreadMetadata]:
        params = {"include_archived": "true", "limit": "200"}
        threads: list[ThreadMetadata] = []
        while True:
            response = await self.http.get("/upstream/app/v1/threads", params=params)
            response.raise_for_status()
            page = response.json()
            for row in page.get("threads", []):
                metadata = ThreadMetadata.model_validate(row)
                summary = await self.http.get(f"/upstream/history/v1/sessions/{metadata.thread_id}/summary")
                if summary.status_code == httpx.codes.NOT_FOUND:
                    continue
                summary.raise_for_status()
                history_sandbox = (summary.json().get("sandbox") or {}).get("name")
                if sandbox is None or history_sandbox == sandbox:
                    threads.append(metadata)
            page_token = page.get("nextPageToken")
            if not page_token:
                return threads
            params["page_token"] = page_token
