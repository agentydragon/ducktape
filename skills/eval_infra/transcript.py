"""JSONL transcript via AF's standard `HistoryProvider` interface.

`JsonlTranscriptProvider` subclasses `InMemoryHistoryProvider` so it can act
as a drop-in replacement for the auto-attached in-memory history (preserving
multi-turn coherence inside an `Agent.run()`) while *also* writing every
newly-stored Message to a JSONL audit log via `Message.to_json()`.

Pair this with `Agent(require_per_service_call_history_persistence=True)`
so AF calls `save_messages` after every LLM round-trip — that's how the
transcript stays current as the agent runs (rather than only being flushed
at the end). All three middleware pipelines suppress `MiddlewareTermination`
internally, so termination via tool-side middleware doesn't drop the final
batch either.

The convenience factory `JsonlTranscriptProvider.opened(path)` opens the
JSONL file and yields a configured provider — saves callers from juggling
the file handle.
"""

from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, Self

from agent_framework import InMemoryHistoryProvider, Message


class JsonlTranscriptProvider(InMemoryHistoryProvider):
    """`InMemoryHistoryProvider` that writes each newly-stored Message to JSONL.

    Tracks a per-instance write cursor against the session's stored messages so
    repeated `save_messages` calls (per-service-call persistence mode) emit only
    the delta — no duplicates even when AF re-passes earlier inputs.
    """

    def __init__(self, log_file: IO[str], source_id: str | None = None) -> None:
        super().__init__(source_id=source_id)
        self._log_file = log_file
        self._written_count = 0

    @classmethod
    @contextmanager
    def opened(cls, path: Path) -> Iterator[Self]:
        """Open `path` for writing and yield a provider bound to the file."""
        with path.open("w") as log_file:
            yield cls(log_file)

    async def save_messages(
        self, session_id: str | None, messages: Sequence[Message], *, state: dict[str, Any] | None = None, **kwargs: Any
    ) -> None:
        await super().save_messages(session_id, messages, state=state, **kwargs)
        if state is None:
            return
        all_messages: list[Message] = state.get("messages", [])
        for msg in all_messages[self._written_count :]:
            self._log_file.write(msg.to_json() + "\n")
        self._written_count = len(all_messages)
        self._log_file.flush()
