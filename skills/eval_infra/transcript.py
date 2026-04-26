"""JSONL transcript via AF's standard `HistoryProvider` interface.

`JsonlTranscriptProvider` subclasses `InMemoryHistoryProvider` so it can act
as a drop-in replacement for the auto-attached in-memory history (preserving
multi-turn coherence inside an `Agent.run()`) while *also* writing every
newly-stored Message to a JSONL audit log via `Message.to_json()`.

By default AF calls `save_messages` once at the end of each `agent.run()`
(or per LLM round-trip when the agent is constructed with
`require_per_service_call_history_persistence=True`) — both call paths leave
the transcript intact even when middleware raises `MiddlewareTermination`,
because all three middleware pipelines suppress that exception internally.

AF's "instructions" (system prompt) do not flow through the Message stream,
so the convenience factory `JsonlTranscriptProvider.opened(path, ...)` opens
the JSONL file, seeds it with a synthetic `Message("system", [...])` line,
and yields a configured provider — used by FL and RE rollouts.
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
    def opened(cls, path: Path, *, system_prompt: str) -> Iterator[Self]:
        """Open `path` for writing, seed with a system Message, yield a provider.

        Single-call replacement for the open-file + manual-system-seed +
        construct-provider boilerplate every rollout repeats.
        """
        with path.open("w") as log_file:
            log_file.write(Message("system", [system_prompt]).to_json() + "\n")
            log_file.flush()
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
