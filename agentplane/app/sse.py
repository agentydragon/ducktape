"""Server-sent events: the frame encoding and the response every SSE route returns."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi.responses import StreamingResponse


def frame(event: str, data: object, *, event_id: int | None = None) -> bytes:
    id_line = "" if event_id is None else f"id: {event_id}\n"
    return f"event: {event}\n{id_line}data: {json.dumps(data)}\n\n".encode()


def sse_response(frames: AsyncIterator[bytes]) -> StreamingResponse:
    # X-Accel-Buffering: a buffering reverse proxy would hold frames back until the stream ends.
    return StreamingResponse(
        frames, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
