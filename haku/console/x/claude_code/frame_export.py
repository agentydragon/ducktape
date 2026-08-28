"""One recorded session's frames, redacted, in the form the recorded-frame tests already read.

The other capture route reads the CLI's stdout in a throwaway directory, which can only record a
session somebody ran for the purpose; the shapes worth pinning — a subagent, a backgrounded `Bash`,
a monitor loop polling it — exist in `session_frames` and nowhere else.

**The format is <claude_code/testdata/diverse_session.jsonl>'s, not a new one.** One JSON object
per line, `frame` carrying the payload and `t` its offset in seconds from the first exported frame,
`original_bytes` on a record redaction shrank. Two of that file's properties do not survive the
change of route:

- **A record's index is its `frame_seq`.** The table's own numbers are database-assigned with gaps
  that mean nothing, and this drops rows besides, so renumbering is what keeps the fixture's
  convention true. Only the order was ever load-bearing.
- **There is no `raw_stdout_line`.** A line that did not parse never became a row, so this route
  cannot see one; `test_diverse_session.py` remains where that hazard is pinned.

**What is exported is what the fold reads** — `reprojection.foldable_frames`, so the console's own
`setup_output` rows are left out, which is also the exclusion adoption replays under.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.conversation import reprojection
from haku.console.conversation.conversation_event import Json
from haku.console.database_schema import SessionFrame
from haku.console.x.claude_code.redaction import Pseudonyms, redact


@dataclass(frozen=True, slots=True)
class ExportedSession:
    session_id: UUID
    records: tuple[dict[str, Json], ...]

    def lines(self) -> Iterator[str]:
        """The fixture itself — one record per line, spaceless, so the file is diffed rather than
        read."""
        return (json.dumps(record, separators=(",", ":")) for record in self.records)

    def summary(self) -> str:
        """One line saying what came out, so an operator sees the export ran before reading it."""
        return f"session {self.session_id}: {len(self.records)} frame(s)"


async def export_session(db: AsyncSession, session_id: UUID) -> ExportedSession:
    """Every foldable frame of one session, redacted, oldest first."""
    frames = (await db.scalars(reprojection.foldable_frames(session_id))).all()
    return ExportedSession(session_id=session_id, records=tuple(_records(frames)))


def _records(frames: Sequence[SessionFrame]) -> Iterator[dict[str, Json]]:
    """One record per frame, sharing one `Pseudonyms` so an identifier means the same thing twice."""
    pseudonyms = Pseudonyms()
    for frame in frames:
        redacted = redact(frame.payload, pseudonyms)
        record: dict[str, Json] = {
            "t": round((frame.created_at - frames[0].created_at).total_seconds(), 4),
            # Keep the outer bridge class and wire position beside the untouched native frame.
            # In particular, do not replace `bridge_kind` with payload["type"]: a future JSON-RPC
            # method must remain forensic data, not a database discriminator.
            "bridge_kind": frame.kind,
            "wire_seq": frame.runner_seq,
            "frame": redacted,
        }
        if (original := len(_encoded(frame.payload))) > len(_encoded(redacted)):
            record["original_bytes"] = original
        yield record


def _encoded(payload: dict[str, Json]) -> str:
    return json.dumps(payload, separators=(",", ":"))
