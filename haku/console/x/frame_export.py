"""One recorded session's frames, redacted, in the form the recorded-frame tests already read.

The capture route that produced <claude_code/testdata/diverse_session.jsonl> was the CLI's stdout
in a throwaway directory, which can only ever record a session somebody ran for the purpose. The
shapes worth pinning next are the ones a console session produces — a subagent, a backgrounded
`Bash`, a monitor loop polling it — and those exist in `session_frames` and nowhere else. This is
the second route to the same file format.

**The format is that fixture's, not a new one.** One JSON object per line, `frame` carrying the
payload and `t` its offset in seconds from the first exported frame, `original_bytes` on a record
redaction shrank. Two of that file's properties do not survive the change of route, and their
absence is the honest reading rather than a gap:

- **A record's index is its `frame_seq`.** The table's own numbers are database-assigned with gaps
  that mean nothing, and this drops rows besides, so renumbering is what keeps the fixture's
  convention true. Only the order was ever load-bearing.
- **There is no `raw_stdout_line`.** A line that did not parse never became a row, so this route
  cannot see one; `test_diverse_session.py` remains where that hazard is pinned.

**What is exported is what the fold reads** — `reprojection.foldable_frames`, so the console's own
two authored row kinds are left out, which is also the set adoption replays under.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from haku.console.database_schema import SessionFrame
from haku.console.x import reprojection
from haku.console.x.claude_code.redaction import Pseudonyms, redact
from haku.console.x.conversation_events import Json


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
        counted = Counter(_kind(record) for record in self.records)
        return (
            f"session {self.session_id}: {len(self.records)} frame(s) — "
            f"{' '.join(f'{kind}×{count}' for kind, count in counted.most_common()) or 'nothing'}"
        )


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
            "frame": redacted,
        }
        if (original := len(_encoded(frame.payload))) > len(_encoded(redacted)):
            record["original_bytes"] = original
        yield record


def _encoded(payload: dict[str, Json]) -> str:
    return json.dumps(payload, separators=(",", ":"))


def _kind(record: dict[str, Json]) -> str:
    frame = record["frame"]
    kind = frame.get("type") if isinstance(frame, dict) else None
    return kind if isinstance(kind, str) else "<untyped>"
