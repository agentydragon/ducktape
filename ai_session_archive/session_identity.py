"""Create stable archive session identities for physical source paths."""

from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.orm import Session as DbSession

from ai_session_archive.database import MachineRow, SessionRow
from ai_session_archive.models import Harness, SessionKind
from ai_session_archive.source_paths import HarnessRoot, SourceLocation, classify_source_path


def _session_id_for_path(machine_id: str, harness: Harness, source_path: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"ai-session:{machine_id}:{harness}:{source_path}")


class SourceSessionIdentity:
    """Resolve source paths inside the caller's transaction."""

    def __init__(self, machine_id: str, roots: tuple[HarnessRoot, ...]):
        self._machine_id = machine_id
        self._roots = tuple(sorted(roots, key=lambda root: len(root.path.parts), reverse=True))

    def ensure(self, db: DbSession, location: SourceLocation, inferred: bool = False) -> SessionRow:
        # Inventories for distinct snapshots can discover the same physical session concurrently.
        db.get(MachineRow, self._machine_id, with_for_update=True)
        session_id = _session_id_for_path(self._machine_id, location.harness, location.source_path)
        row = db.get(SessionRow, session_id)
        if row is not None:
            if not inferred and row.identity_inferred:
                row.identity_inferred = False
                row.source_relative_path = location.relative_path.as_posix()
                if row.native_session_id_inferred:
                    row.native_session_id = location.native_session_id
                if location.parent_source_path is not None:
                    row.parent_session_id = _session_id_for_path(
                        self._machine_id, location.harness, location.parent_source_path
                    )
                    row.parent_link_kind = "claude_path_inferred"
            return row
        parent_id = None
        parent_link_kind = None
        if not inferred and location.parent_source_path is not None:
            parent_id = _session_id_for_path(self._machine_id, location.harness, location.parent_source_path)
            parent_link_kind = "claude_path_inferred"
            parent_location = classify_source_path(location.parent_source_path, self._roots)
            if parent_location is not None:
                self.ensure(db, parent_location, inferred=True)
        row = SessionRow(
            id=session_id,
            machine_id=self._machine_id,
            harness=location.harness.value,
            source_path=location.source_path,
            source_relative_path=location.relative_path.as_posix(),
            native_session_id=location.native_session_id,
            native_session_id_inferred=True,
            parent_session_id=parent_id,
            parent_link_kind=parent_link_kind,
            identity_inferred=inferred,
            session_kind=(
                SessionKind.SUBAGENT.value
                if location.harness == Harness.CLAUDE and location.parent_source_path is not None
                else SessionKind.MAIN.value
                if location.harness == Harness.CLAUDE and inferred
                else SessionKind.UNKNOWN.value
            ),
        )
        db.add(row)
        db.flush()
        return row
