"""Ground truth API routes for viewing snapshots and issues."""

from __future__ import annotations

import io
import tarfile
from collections import Counter, defaultdict
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import selectinload

from props.core.db.models import (
    CriticScopeExpectedToRecall,
    FalsePositive,
    FileSet,
    FileSetMember,
    OccurrenceRangeORM,
    Snapshot,
    SnapshotFile,
    TruePositive,
    TruePositiveOccurrenceORM,
)
from props.core.db.session import get_session
from props.core.ids import SnapshotSlug
from props.core.models.true_positive import LineRange
from props.core.splits import Split

router = APIRouter()


# --- Response Models ---


class SnapshotSummary(BaseModel):
    """Summary info for a snapshot in list view."""

    slug: SnapshotSlug
    split: Split
    tp_count: int
    fp_count: int
    created_at: datetime


class SnapshotsListResponse(BaseModel):
    """Response for listing snapshots."""

    snapshots: list[SnapshotSummary]


class FileLocationInfo(BaseModel):
    """File with optional line ranges."""

    path: str
    ranges: list[LineRange] | None


class OccurrenceInfo(BaseModel):
    """Unified occurrence info for both TPs and FPs."""

    occurrence_id: str
    files: list[FileLocationInfo]
    note: str | None
    graders_match_only_if_reported_on: list[str] | None
    # TP-specific fields
    critic_scopes_expected_to_recall: list[list[str]] | None = None
    # FP-specific fields
    relevant_files: list[str] | None = None


class TpInfo(BaseModel):
    """True positive issue info."""

    tp_id: str
    rationale: str
    occurrences: list[OccurrenceInfo]
    created_at: datetime


class FpInfo(BaseModel):
    """False positive issue info."""

    fp_id: str
    rationale: str
    occurrences: list[OccurrenceInfo]
    created_at: datetime


class SnapshotDetailResponse(BaseModel):
    """Detailed snapshot info with all issues."""

    slug: SnapshotSlug
    split: Split
    created_at: datetime
    true_positives: list[TpInfo]
    false_positives: list[FpInfo]


# --- Helper Functions ---


def _build_file_locations_from_ranges(ranges: list[OccurrenceRangeORM]) -> list[FileLocationInfo]:
    """Convert ORM ranges to FileLocationInfo list."""
    by_file: dict[str, list[LineRange]] = defaultdict(list)
    for range_orm in ranges:
        by_file[str(range_orm.file_path)].append(
            LineRange(
                start_line=range_orm.start_line,
                end_line=range_orm.end_line if range_orm.end_line != range_orm.start_line else None,
                note=range_orm.note,
            )
        )
    return [FileLocationInfo(path=path, ranges=ranges_list) for path, ranges_list in sorted(by_file.items())]


def _get_critic_scopes_expected_to_recall_paths(occ: TruePositiveOccurrenceORM) -> list[list[str]]:
    """Get critic_scopes_expected_to_recall paths from occurrence relationship."""
    return [
        sorted(m.file_path for m in scope.file_set.members)
        for scope in occ.critic_scopes_expected_to_recall
        if scope.file_set
    ]


def _get_matchable_files(session, snapshot_slug: SnapshotSlug, files_hash: str | None) -> list[str] | None:
    """Get graders_match_only_if_reported_on paths from hash.

    NOTE: This function is retained for potential future use but is no longer called in hot paths.
    The main endpoint pre-fetches all matchable files in bulk to avoid N+1 queries.
    """
    if not files_hash:
        return None
    members = (
        session.query(FileSetMember.file_path)
        .filter_by(snapshot_slug=snapshot_slug, files_hash=files_hash)
        .order_by(FileSetMember.file_path)
        .all()
    )
    return [m.file_path for m in members]


# --- Endpoints ---


@router.get("/snapshots")
def list_snapshots() -> SnapshotsListResponse:
    """List all snapshots with issue counts."""
    with get_session() as session:
        # Get snapshots with TP/FP counts
        snapshots = session.query(Snapshot).order_by(Snapshot.created_at.desc()).all()

        # Count TPs and FPs per snapshot
        tp_counts: dict[SnapshotSlug, int] = {
            row[0]: row[1]
            for row in session.query(TruePositive.snapshot_slug, func.count(TruePositive.tp_id))
            .group_by(TruePositive.snapshot_slug)
            .all()
        }
        fp_counts: dict[SnapshotSlug, int] = {
            row[0]: row[1]
            for row in session.query(FalsePositive.snapshot_slug, func.count(FalsePositive.fp_id))
            .group_by(FalsePositive.snapshot_slug)
            .all()
        }

        return SnapshotsListResponse(
            snapshots=[
                SnapshotSummary(
                    slug=s.slug,
                    split=s.split,
                    tp_count=tp_counts.get(s.slug, 0),
                    fp_count=fp_counts.get(s.slug, 0),
                    created_at=s.created_at,
                )
                for s in snapshots
            ]
        )


@router.get("/snapshots/{snapshot_slug:path}")
def get_snapshot_detail(snapshot_slug: SnapshotSlug) -> SnapshotDetailResponse:
    """Get detailed snapshot info with all TPs and FPs."""
    slug = snapshot_slug

    with get_session() as session:
        snapshot = session.query(Snapshot).filter_by(slug=slug).first()
        if not snapshot:
            raise HTTPException(status_code=404, detail=f"Snapshot not found: {slug}")

        # Get TPs with eager loading
        tps = (
            session.query(TruePositive)
            .filter_by(snapshot_slug=slug)
            .options(
                selectinload(TruePositive.occurrences)
                .selectinload(TruePositiveOccurrenceORM.critic_scopes_expected_to_recall)
                .selectinload(CriticScopeExpectedToRecall.file_set)
                .selectinload(FileSet.members)
            )
            .order_by(TruePositive.tp_id)
            .all()
        )

        # Get FPs with eager loading
        fps = (
            session.query(FalsePositive)
            .filter_by(snapshot_slug=slug)
            .options(selectinload(FalsePositive.occurrences))
            .order_by(FalsePositive.fp_id)
            .all()
        )

        # Pre-fetch all matchable files to avoid N+1 queries
        # Collect all unique graders_match_only_if_reported_on hashes from both TPs and FPs
        # Note: whole-snapshot occurrences have graders_match_only_if_reported_on=None (no file filter)
        file_set_hashes = {
            occ.graders_match_only_if_reported_on
            for issues in (tps, fps)
            for issue in issues
            for occ in issue.occurrences
            if occ.graders_match_only_if_reported_on
        }

        # Bulk fetch all file set members for these hashes
        matchable_files_by_hash: dict[str, list[str]] = defaultdict(list)
        if file_set_hashes:
            members = (
                session.query(FileSetMember.files_hash, FileSetMember.file_path)
                .filter(FileSetMember.snapshot_slug == slug, FileSetMember.files_hash.in_(file_set_hashes))
                .order_by(FileSetMember.files_hash, FileSetMember.file_path)
                .all()
            )
            for files_hash, file_path in members:
                matchable_files_by_hash[files_hash].append(file_path)

        # Convert TPs
        tp_infos = []
        for tp in tps:
            tp_occ_infos: list[OccurrenceInfo] = []
            for occ in tp.occurrences:
                matchable_files = (
                    matchable_files_by_hash.get(occ.graders_match_only_if_reported_on)
                    if occ.graders_match_only_if_reported_on
                    else None
                )
                tp_occ_infos.append(
                    OccurrenceInfo(
                        occurrence_id=occ.occurrence_id,
                        files=_build_file_locations_from_ranges(occ.ranges),
                        note=occ.note,
                        graders_match_only_if_reported_on=matchable_files,
                        critic_scopes_expected_to_recall=_get_critic_scopes_expected_to_recall_paths(occ),
                    )
                )
            tp_infos.append(
                TpInfo(tp_id=tp.tp_id, rationale=tp.rationale, occurrences=tp_occ_infos, created_at=tp.created_at)
            )

        # Convert FPs
        fp_infos = []
        for fp in fps:
            fp_occ_infos: list[OccurrenceInfo] = []
            for fp_occ in fp.occurrences:
                matchable_files = (
                    matchable_files_by_hash.get(fp_occ.graders_match_only_if_reported_on)
                    if fp_occ.graders_match_only_if_reported_on
                    else None
                )
                fp_occ_infos.append(
                    OccurrenceInfo(
                        occurrence_id=fp_occ.occurrence_id,
                        files=_build_file_locations_from_ranges(fp_occ.ranges),
                        note=fp_occ.note,
                        graders_match_only_if_reported_on=matchable_files,
                        relevant_files=sorted(str(rf.file_path) for rf in fp_occ.relevant_file_orms),
                    )
                )
            fp_infos.append(
                FpInfo(fp_id=fp.fp_id, rationale=fp.rationale, occurrences=fp_occ_infos, created_at=fp.created_at)
            )

        return SnapshotDetailResponse(
            slug=snapshot.slug,
            split=snapshot.split,
            created_at=snapshot.created_at,
            true_positives=tp_infos,
            false_positives=fp_infos,
        )


# --- File Browser Endpoints ---


class FileTreeNode(BaseModel):
    """Node in file tree (file or directory)."""

    path: str
    name: str
    is_dir: bool
    tp_count: int = 0
    fp_count: int = 0
    children: list[FileTreeNode] | None = None  # None for files, list for directories


class FileTreeResponse(BaseModel):
    """Directory tree with issue counts."""

    tree: list[FileTreeNode]


@router.get("/snapshots/{snapshot_slug:path}/tree")
def get_snapshot_tree(snapshot_slug: SnapshotSlug) -> FileTreeResponse:
    """Get directory tree with issue occurrence counts."""
    slug = snapshot_slug

    with get_session() as session:
        snapshot = session.query(Snapshot).filter_by(slug=slug).first()
        if not snapshot:
            raise HTTPException(status_code=404, detail=f"Snapshot not found: {slug}")

        # Get all snapshot files
        snapshot_files_rows = (
            session.query(SnapshotFile.relative_path)
            .filter_by(snapshot_slug=slug)
            .order_by(SnapshotFile.relative_path)
            .all()
        )
        snapshot_files = {row.relative_path for row in snapshot_files_rows}

        # Get TP occurrences with file locations
        tps = (
            session.query(TruePositive)
            .filter_by(snapshot_slug=slug)
            .options(selectinload(TruePositive.occurrences))
            .all()
        )

        # Get FP occurrences with file locations
        fps = (
            session.query(FalsePositive)
            .filter_by(snapshot_slug=slug)
            .options(selectinload(FalsePositive.occurrences))
            .all()
        )

        # Count occurrences per file
        tp_counts_by_file = Counter(
            str(range_orm.file_path) for tp in tps for occ in tp.occurrences for range_orm in occ.ranges
        )
        fp_counts_by_file = Counter(
            str(range_orm.file_path) for fp in fps for occ in fp.occurrences for range_orm in occ.ranges
        )

        # Build tree structure
        root_nodes: dict[str, FileTreeNode] = {}

        def ensure_path(path: str) -> FileTreeNode:
            """Ensure path and all parents exist in tree."""
            if path in root_nodes:
                return root_nodes[path]

            parts = path.split("/")
            if len(parts) == 1:
                # Root level file/dir
                node = FileTreeNode(path=path, name=path, is_dir=False, children=None)
                root_nodes[path] = node
                return node

            # Need to create parent
            parent_path = "/".join(parts[:-1])
            parent = ensure_path(parent_path)

            # Mark parent as directory
            if parent.children is None:
                parent.is_dir = True
                parent.children = []

            # Create this node
            node = FileTreeNode(path=path, name=parts[-1], is_dir=False, children=None)
            parent.children.append(node)
            root_nodes[path] = node
            return node

        # Add all snapshot files to tree
        for file_path in sorted(snapshot_files):
            ensure_path(file_path)

        # Propagate counts up the tree
        def propagate_counts(node: FileTreeNode) -> tuple[int, int]:
            """Return (tp_count, fp_count) for this node and set on node."""
            if not node.is_dir:
                # Leaf file - use direct counts
                node.tp_count = tp_counts_by_file.get(node.path, 0)
                node.fp_count = fp_counts_by_file.get(node.path, 0)
                return (node.tp_count, node.fp_count)

            # Directory - sum children
            total_tp = 0
            total_fp = 0
            if node.children:
                for child in node.children:
                    child_tp, child_fp = propagate_counts(child)
                    total_tp += child_tp
                    total_fp += child_fp

            node.tp_count = total_tp
            node.fp_count = total_fp
            return (total_tp, total_fp)

        # Get root-level nodes
        root_level = [node for path, node in root_nodes.items() if "/" not in path]

        # Propagate counts
        for node in root_level:
            propagate_counts(node)

        return FileTreeResponse(tree=root_level)


class FileContentResponse(BaseModel):
    """File content from snapshot."""

    path: str
    content: str
    line_count: int


@router.get("/snapshots/{snapshot_slug:path}/files/{file_path:path}")
def get_snapshot_file(snapshot_slug: SnapshotSlug, file_path: str) -> FileContentResponse:
    """Get file content from snapshot tar archive."""
    slug = snapshot_slug

    with get_session() as session:
        snapshot = session.query(Snapshot).filter_by(slug=slug).first()
        if not snapshot:
            raise HTTPException(status_code=404, detail=f"Snapshot not found: {slug}")

        if not snapshot.content:
            raise HTTPException(status_code=404, detail=f"Snapshot has no content: {slug}")

        # Check if file exists in snapshot
        snapshot_file = session.query(SnapshotFile).filter_by(snapshot_slug=slug, relative_path=file_path).first()
        if not snapshot_file:
            raise HTTPException(status_code=404, detail=f"File not found in snapshot: {file_path}")

        # Extract file from tar
        buffer = io.BytesIO(snapshot.content)
        try:
            with tarfile.open(fileobj=buffer, mode="r") as tar:
                try:
                    member = tar.getmember(file_path)
                    file_obj = tar.extractfile(member)
                    if file_obj is None:
                        raise HTTPException(status_code=400, detail=f"Cannot extract file: {file_path}")

                    content_bytes = file_obj.read()
                    # Decode as UTF-8, replace invalid chars
                    content = content_bytes.decode("utf-8", errors="replace")

                    return FileContentResponse(path=file_path, content=content, line_count=snapshot_file.line_count)
                except KeyError:
                    raise HTTPException(status_code=404, detail=f"File not in tar archive: {file_path}") from None
        except tarfile.TarError as e:
            raise HTTPException(status_code=500, detail=f"Error reading tar archive: {e}") from e
