"""Retention holds: a holder keeps a Sandbox incarnation from deletion until it has confirmed each
held Session through the seal cursor that ends the Session in that incarnation.

The holds are one annotation on the Sandbox CR, like the rest of the Sandbox Service's lifecycle
state, so they are deleted with the incarnation they hold and every write is guarded by the CR's
resourceVersion, the same guard deletion uses.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from agentplane.sandbox_service.protocol_pb2 import Hold

# gazelle:include_dep @pypi//protobuf

# Bounds a Session ID or holder name, which key the stored annotation.
MAX_HOLD_KEY_LENGTH = 128


class SessionHolds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    confirmed_through: dict[str, int] = Field(
        description="Each holder's confirmed cursor in this Session, 0 before its first confirmation."
    )
    seal_cursor: int | None = Field(
        description="The Session's final cursor in this incarnation, recorded when the runner seals it at "
        "Sandbox teardown. Nothing records a seal yet, so no hold is satisfied before it is released."
    )


class RetentionHolds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    sessions: dict[str, SessionHolds] = Field(description="Keyed by the Session ID the holders named.")

    def holds(self) -> list[Hold]:
        return [
            Hold(session_id=session_id, holder=holder, confirmed_through=cursor, seal_cursor=session.seal_cursor)
            for session_id, session in sorted(self.sessions.items())
            for holder, cursor in sorted(session.confirmed_through.items())
        ]

    def blocking(self) -> list[Hold]:
        """The holds that keep the Sandbox from deletion: every one not confirmed through its seal."""
        return [
            hold
            for hold in self.holds()
            if not hold.HasField("seal_cursor") or hold.confirmed_through < hold.seal_cursor
        ]

    def hold(self, session_id: str, holder: str) -> Hold | None:
        session = self.sessions.get(session_id)
        if session is None or holder not in session.confirmed_through:
            return None
        return Hold(
            session_id=session_id,
            holder=holder,
            confirmed_through=session.confirmed_through[holder],
            seal_cursor=session.seal_cursor,
        )

    def place(self, session_id: str, holder: str) -> RetentionHolds:
        session = self.sessions.get(session_id, SessionHolds(confirmed_through={}, seal_cursor=None))
        if holder in session.confirmed_through:
            return self
        return self._with(
            session_id, session.model_copy(update={"confirmed_through": {**session.confirmed_through, holder: 0}})
        )

    def confirm(self, session_id: str, holder: str, through_cursor: int) -> RetentionHolds:
        """A confirmation never moves backwards: a retry may arrive after a later one."""
        session = self.sessions[session_id]
        if through_cursor <= session.confirmed_through[holder]:
            return self
        return self._with(
            session_id,
            session.model_copy(update={"confirmed_through": {**session.confirmed_through, holder: through_cursor}}),
        )

    def release(self, session_id: str, holder: str) -> RetentionHolds:
        session = self.sessions.get(session_id)
        if session is None or holder not in session.confirmed_through:
            return self
        remaining = {name: cursor for name, cursor in session.confirmed_through.items() if name != holder}
        if not remaining and session.seal_cursor is None:
            return RetentionHolds(sessions={key: value for key, value in self.sessions.items() if key != session_id})
        return self._with(session_id, session.model_copy(update={"confirmed_through": remaining}))

    def _with(self, session_id: str, session: SessionHolds) -> RetentionHolds:
        return RetentionHolds(sessions={**self.sessions, session_id: session})


def read_holds(raw: str | None) -> RetentionHolds:
    """The stored annotation; a Sandbox without it has no holds."""
    return RetentionHolds(sessions={}) if raw is None else RetentionHolds.model_validate_json(raw)
