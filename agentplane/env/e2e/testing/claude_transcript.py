"""Wait for the native transcript a real Claude process must retain before a crash."""

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

from tenacity import AsyncRetrying, retry_if_exception_type, stop_after_delay, wait_fixed

from agentplane.env.e2e.testing import events
from agentplane.proto import event_log_pb2, event_pb2, session_pb2

# gazelle:include_dep @pypi//protobuf


async def wait_for_claude_transcript(
    spec: session_pb2.SessionSpec,
    session_id: str,
    entries: Sequence[event_log_pb2.EventEntry],
    state_dir: Path,
    text: str,
) -> None:
    """Resume tests require the native transcript to exist before the abrupt container death."""
    if spec.placement.environment_hosted.harness != session_pb2.HARNESS_CLAUDE:
        return
    inits = [
        frame
        for frame in (
            json.loads(entry.event.native.line)
            for entry in events.of_kind(entries, "native")
            if entry.event.native.direction == event_pb2.DIRECTION_FROM_HARNESS
        )
        if frame.get("type") == "system" and frame.get("subtype") == "init"
    ]
    native_id = inits[-1]["session_id"]
    directory = state_dir / "sessions" / session_id / "claude" / "projects"

    def recorded() -> bool:
        for path in directory.glob(f"*/{native_id}.jsonl"):
            for line in path.read_text().splitlines():
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue  # Claude may be writing the last line.
                if entry.get("type") == "assistant" and text in json.dumps(entry.get("message")):
                    return True
        return False

    # Claude owns the file and does not publish a flush notification.
    async for attempt in AsyncRetrying(
        stop=stop_after_delay(10), wait=wait_fixed(0.01), retry=retry_if_exception_type(AssertionError), reraise=True
    ):
        with attempt:
            assert await asyncio.to_thread(recorded), "Claude did not persist its resume transcript"
