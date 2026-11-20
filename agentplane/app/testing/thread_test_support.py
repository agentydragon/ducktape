"""HTTP observations and mutations of real Go app Threads."""

from uuid import UUID

import httpx
from google.protobuf.json_format import MessageToDict
from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.app import app_service_pb2
from agentplane.app.testing.models import ThreadMetadata


class ThreadHTTP:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self.http = http

    async def wait_for_history(self, thread_id: UUID) -> None:
        """Wait until History has a summary for this Session, without assuming App metadata exists."""
        async for attempt in AsyncRetrying(
            stop=stop_after_delay(15), wait=wait_fixed(0.05), retry=retry_if_exception_type(AssertionError)
        ):
            with attempt:
                summary = await self.http.get(f"/upstream/history/v1/sessions/{thread_id}/summary")
                assert summary.status_code == 200, summary.text

    async def rename(self, thread_id: UUID, name: str) -> None:
        request = app_service_pb2.RenameThreadRequest(thread_id=str(thread_id), name=name)
        response = await self.http.patch(f"/upstream/app/v1/threads/{thread_id}", json=MessageToDict(request))
        response.raise_for_status()

    async def archive(self, thread_id: UUID) -> None:
        request = app_service_pb2.ThreadMetadataTarget(thread_id=str(thread_id))
        response = await self.http.post(f"/upstream/app/v1/threads/{thread_id}/archive", json=MessageToDict(request))
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
