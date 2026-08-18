"""Every stored prompt names the surface it arrived through. **Destructive.**

`PromptBody.origin` is required in this release, so a `prompt_enqueued` body without the key is one
no reader can parse. Every row written before this release is such a row — the field is new here —
and there is no honest backfill: an invented origin is read by cross-surface visibility as "this
prompt arrived through the SPA", which is the answer that makes an attached room repost history at
the operator. Destroying them is authorized (operator, 2026-08-17). The prompt text itself is not
destroyed with them: `session_messages` holds it, and the deleted row is the event-stream copy.

**The CHECK is why this is not merely a cleanup.** Unlike `0068`, this migration cannot wait for
every pod to run an image that names an origin, because this is the release that introduces one:
the previous image writes bodies without the key, and under `maxUnavailable: 0` it keeps serving
while the new image applies this and comes up. The delete alone would leave that window free to
write a row the new image chokes on forever. The constraint turns it into a rejected INSERT — a
failure the operator sees and retries, over a session whose transcript stops rendering.

Ordering, in one direction: the rows go before the constraint arrives, since a CHECK cannot be
added over rows that violate it.

`jsonb_exists(body, 'origin')` rather than `body ? 'origin'`: the same operator, spelled so it
carries through DDL and driver parameter handling unambiguously.

Revision ID: 0078
Revises: 0077
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0078"
down_revision: str | None = "0077"
branch_labels: str | None = None
depends_on: str | None = None

_EVENTS = "session_events"
_ORIGIN = "ck_session_events_prompt_origin"


def upgrade() -> None:
    op.execute(sa.text(f"DELETE FROM {_EVENTS} WHERE kind = 'prompt_enqueued' AND NOT jsonb_exists(body, 'origin')"))
    op.create_check_constraint(_ORIGIN, _EVENTS, "kind <> 'prompt_enqueued' OR jsonb_exists(body, 'origin')")


def downgrade() -> None:
    """Drops the constraint, not the rows — those are the point of the upgrade rather than a step in
    it, and an earlier image reads the table with or without them."""
    op.drop_constraint(_ORIGIN, _EVENTS, type_="check")
