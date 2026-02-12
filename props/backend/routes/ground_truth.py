"""Ground truth API routes for viewing snapshots and issues.

All endpoints require admin access (localhost admin or authenticated admin user).
"""

from __future__ import annotations

import io
import tarfile
from collections import Counter, defaultdict
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session, selectinload

from props.backend.auth import require_admin_access
from props.backend.deps import AdminDb
from props.core.ids import SnapshotSlug
from props.core.splits import Split
from props.db.models import (
    CriticScopeExpectedToRecall,
    FalsePositive,
    FileSet,
    FileSetMember,
    IssueCluster,
    OccurrenceRangeORM,
    ReportedIssue,
    Snapshot,
    SnapshotFile,
    TruePositive,
    TruePositiveOccurrenceORM,
)
from props.db.snapshots import LocationAnchor

router = APIRouter(dependencies=[Depends(require_admin_access)])


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


class OccurrenceInfo(BaseModel):
    """Unified occurrence info for both TPs and FPs."""

    occurrence_id: str
    locations: list[LocationAnchor]
    note: str | None
    match_file_restriction: list[str] | None
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


def _ranges_to_locations(ranges: list[OccurrenceRangeORM]) -> list[LocationAnchor]:
    """Convert ORM occurrence ranges to flat LocationAnchor list."""
    return [
        LocationAnchor(
            file=str(r.file_path),
            start_line=r.start_line,
            end_line=r.end_line if r.end_line != r.start_line else None,
            note=r.note,
        )
        for r in ranges
    ]


def _get_critic_scopes_expected_to_recall_paths(occ: TruePositiveOccurrenceORM) -> list[list[str]]:
    """Get critic_scopes_expected_to_recall paths from occurrence relationship."""
    return [
        sorted(m.file_path for m in scope.file_set.members)
        for scope in occ.critic_scopes_expected_to_recall
        if scope.file_set
    ]


# --- Endpoints ---


def get_snapshot_or_404(session: Session, snapshot_slug: SnapshotSlug) -> Snapshot:
    """Get snapshot or raise 404."""
    snapshot = session.query(Snapshot).filter_by(slug=snapshot_slug).first()
    if not snapshot:
        raise HTTPException(status_code=404, detail=f"Snapshot not found: {snapshot_slug}")
    return snapshot


@router.get("/snapshots")
def list_snapshots(admin_db: AdminDb) -> SnapshotsListResponse:
    """List all snapshots with issue counts."""
    with admin_db.session() as session:
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


@router.get("/snapshots/{org}/{snapshot_date}")
def get_snapshot_detail(org: str, snapshot_date: str, admin_db: AdminDb) -> SnapshotDetailResponse:
    snapshot_slug = SnapshotSlug(f"{org}/{snapshot_date}")
    """Get detailed snapshot info with all TPs and FPs."""
    with admin_db.session() as session:
        snapshot = get_snapshot_or_404(session, snapshot_slug)

        # Get TPs with eager loading
        tps = (
            session.query(TruePositive)
            .filter_by(snapshot_slug=snapshot_slug)
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
            .filter_by(snapshot_slug=snapshot_slug)
            .options(selectinload(FalsePositive.occurrences))
            .order_by(FalsePositive.fp_id)
            .all()
        )

        # Pre-fetch all matchable files to avoid N+1 queries
        # Collect all unique match_file_restriction hashes from both TPs and FPs
        # Note: whole-snapshot occurrences have match_file_restriction=None (no file filter)
        file_set_hashes = {
            occ.match_file_restriction
            for issues in (tps, fps)
            for issue in issues
            for occ in issue.occurrences
            if occ.match_file_restriction
        }

        # Bulk fetch all file set members for these hashes
        matchable_files_by_hash: dict[str, list[str]] = defaultdict(list)
        if file_set_hashes:
            members = (
                session.query(FileSetMember.files_hash, FileSetMember.file_path)
                .filter(FileSetMember.snapshot_slug == snapshot_slug, FileSetMember.files_hash.in_(file_set_hashes))
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
                    matchable_files_by_hash.get(occ.match_file_restriction) if occ.match_file_restriction else None
                )
                tp_occ_infos.append(
                    OccurrenceInfo(
                        occurrence_id=occ.occurrence_id,
                        locations=_ranges_to_locations(occ.ranges),
                        note=occ.note,
                        match_file_restriction=matchable_files,
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
                    matchable_files_by_hash.get(fp_occ.match_file_restriction)
                    if fp_occ.match_file_restriction
                    else None
                )
                fp_occ_infos.append(
                    OccurrenceInfo(
                        occurrence_id=fp_occ.occurrence_id,
                        locations=_ranges_to_locations(fp_occ.ranges),
                        note=fp_occ.note,
                        match_file_restriction=matchable_files,
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
    children: list[FileTreeNode] | None = Field(default=None, description="None for files, list for directories")


class FileTreeResponse(BaseModel):
    """Directory tree with issue counts."""

    tree: list[FileTreeNode]


@router.get("/snapshots/{org}/{snapshot_date}/tree")
def get_snapshot_tree(org: str, snapshot_date: str, admin_db: AdminDb) -> FileTreeResponse:
    snapshot_slug = SnapshotSlug(f"{org}/{snapshot_date}")
    """Get directory tree with issue occurrence counts."""
    with admin_db.session() as session:
        get_snapshot_or_404(session, snapshot_slug)

        # Get all snapshot files
        snapshot_files_rows = (
            session.query(SnapshotFile.file_path)
            .filter_by(snapshot_slug=snapshot_slug)
            .order_by(SnapshotFile.file_path)
            .all()
        )
        snapshot_files = {row.file_path for row in snapshot_files_rows}

        # Get TP occurrences with file locations
        tps = (
            session.query(TruePositive)
            .filter_by(snapshot_slug=snapshot_slug)
            .options(selectinload(TruePositive.occurrences))
            .all()
        )

        # Get FP occurrences with file locations
        fps = (
            session.query(FalsePositive)
            .filter_by(snapshot_slug=snapshot_slug)
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


@router.get("/snapshots/{org}/{snapshot_date}/files/{file_path:path}")
def get_snapshot_file(org: str, snapshot_date: str, file_path: str, admin_db: AdminDb) -> FileContentResponse:
    snapshot_slug = SnapshotSlug(f"{org}/{snapshot_date}")
    """Get file content from snapshot tar archive."""
    with admin_db.session() as session:
        snapshot = get_snapshot_or_404(session, snapshot_slug)

        if not snapshot.content:
            raise HTTPException(status_code=404, detail=f"Snapshot has no content: {snapshot_slug}")

        # Check if file exists in snapshot
        snapshot_file = session.query(SnapshotFile).filter_by(snapshot_slug=snapshot_slug, file_path=file_path).first()
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


# --- Cluster Endpoints ---


class ClusterMemberResponse(BaseModel):
    """Member of an issue cluster."""

    critique_run_id: str
    critique_issue_id: str
    rationale: str
    issue_rationale: str | None = Field(description="The original reported issue rationale")


class ClusterResponse(BaseModel):
    """Issue cluster with members."""

    cluster_id: str
    rationale: str
    members: list[ClusterMemberResponse]


class ClustersListResponse(BaseModel):
    """All clusters for a snapshot."""

    clusters: list[ClusterResponse]


@router.get("/snapshots/{org}/{snapshot_date}/clusters")
def list_clusters(org: str, snapshot_date: str, admin_db: AdminDb) -> ClustersListResponse:
    snapshot_slug = SnapshotSlug(f"{org}/{snapshot_date}")
    """List all issue clusters for a snapshot with members."""
    with admin_db.session() as session:
        get_snapshot_or_404(session, snapshot_slug)

        clusters = (
            session.query(IssueCluster).filter_by(snapshot_slug=snapshot_slug).order_by(IssueCluster.cluster_id).all()
        )

        # Collect all member issue run IDs to batch-fetch rationales
        all_run_ids = {m.critique_run_id for cluster in clusters for m in cluster.members}

        # Batch fetch reported issue rationales
        issue_rationales: dict[tuple[str, str], str] = {}
        if all_run_ids:
            issues = session.query(ReportedIssue).filter(ReportedIssue.agent_run_id.in_(all_run_ids)).all()
            for issue in issues:
                issue_rationales[(str(issue.agent_run_id), issue.issue_id)] = issue.rationale

        result = []
        for cluster in clusters:
            members = [
                ClusterMemberResponse(
                    critique_run_id=str(m.critique_run_id),
                    critique_issue_id=m.critique_issue_id,
                    rationale=m.rationale,
                    issue_rationale=issue_rationales.get((str(m.critique_run_id), m.critique_issue_id)),
                )
                for m in cluster.members
            ]
            result.append(ClusterResponse(cluster_id=cluster.cluster_id, rationale=cluster.rationale, members=members))

        return ClustersListResponse(clusters=result)
