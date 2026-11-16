"""Follow published transcript versions back to their checkpoint."""

from sqlalchemy.orm import Session

from ai_session_archive.database import FileVersionRow

CHECKPOINT_INTERVAL = 64


def version_lineage(db: Session, version: FileVersionRow, *, max_delta_depth: int) -> list[FileVersionRow]:
    """Return newest-first versions, rejecting missing or cross-session parents."""
    lineage = [version]
    while not lineage[-1].is_checkpoint:
        parent_id = lineage[-1].parent_version_id
        if len(lineage) > max_delta_depth:
            raise RuntimeError(f"file version delta chain exceeds {max_delta_depth}: {version.id}")
        if parent_id is None:
            raise RuntimeError(f"non-checkpoint file version has no parent: {lineage[-1].id}")
        parent = db.get(FileVersionRow, parent_id)
        if parent is None or parent.session_id != version.session_id:
            raise RuntimeError(f"file version has an invalid parent: {lineage[-1].id}")
        lineage.append(parent)
    return lineage
