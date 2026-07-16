"""In-app changelog: entries surfaced to each user until they acknowledge.

Append new entries at the end with the next sequential `id` — ids are the
per-user ack cursor (`changelog_acks.last_acked_id`), so they must be
monotonically increasing and never reused. `GET /state` returns the entries
newer than the caller's ack; the frontend shows them once and acks.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from x.study_casino.models import ChangelogAckRow


class ChangelogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(ge=1)
    date: str = Field(description="ISO date the change shipped.")
    title: str
    items: list[str] = Field(description="Bullet points shown under the title.")


CHANGELOG: list[ChangelogEntry] = [
    ChangelogEntry(
        id=1,
        date="2026-07-16",
        title="Credit system v2: streaks, daily bonus, fairer accounting",
        items=[
            "Credits are now fractional — every second of studying counts (no more losing partial minutes).",
            "Daily streak: each consecutive day you study 5+ minutes adds +1% to all credit earnings, up to +100%.",
            "Every 14 streak days banks a rest day that protects your streak across a single missed day.",
            "First 5 minutes studied each day award a +30 credit bonus (streak-multiplied).",
            "Prize costs and token balances doubled to match the boosted earning rates — "
            "your saved tokens kept their purchasing power.",
        ],
    )
]

LATEST_CHANGELOG_ID = CHANGELOG[-1].id


def entries_after(last_acked_id: int) -> list[ChangelogEntry]:
    return [entry for entry in CHANGELOG if entry.id > last_acked_id]


def get_or_create_ack(s: Session, username: str) -> ChangelogAckRow:
    row = s.get(ChangelogAckRow, username)
    if row is None:
        row = ChangelogAckRow(user_id=username, last_acked_id=0)
        s.add(row)
        s.flush()
    return row
