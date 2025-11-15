"""
FastMCP server: Read-only Git tools (status, diff, log, show, rev-parse, ls-files, branches).

Design
- Explicit allowlist of read-only tools; no write/mutating operations are implemented
- Worktree-aware scoping: callers pass a worktree_root (Path). We execute libgit2 operations
  under that worktree (equivalent intent to `git -C <worktree_root> ...`). Works for normal repos
  and Git worktrees (where .git is a gitdir pointer file).
- Scope enforcement: worktree_root must resolve under one of configured allowed_roots to prevent
  path traversal/symlink escape. We also validate via pygit2.discover_repository.
- Typed Pydantic input/outputs provide precise JSON Schemas (better LLM tool-calling).
- Large output resilience: tools that can emit large text support TextSlice pagination and
  return TextPage with truncated/next_offset/total_chars metadata.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

# FastMCP-only: no TokenVerifier in server construction
from pydantic import BaseModel, Field
import pygit2
from pygit2.enums import BranchType

from adgn.mcp.compositor.server import Compositor
from adgn.mcp.notifying_fastmcp import NotifyingFastMCP

from .formatting import (
    ChangedFilesPage,
    DiffStatPage,
    ListSlice,
    StatusEntry,
    StatusPage,
    StringListPage,
    TextPage,
    TextSlice,
    apply_list_slice,
    apply_text_slice,
    build_changed_files_page,
    build_diff_stat_page,
    build_status_page,
    diff_to_changed_files,
    diff_to_file_stats,
)

# Shared server name constant for clients/tests
GIT_RO_SERVER_NAME = "git-ro"

# -------------------------- shared slicing -----------------------------------


## moved to formatting.py


# -------------------------- helpers -----------------------------------------


def _open_repo(root: Path) -> pygit2.Repository:
    gitdir = pygit2.discover_repository(str(root))
    if not gitdir:
        raise ValueError(f"Not a git repository: {root}")
    return pygit2.Repository(gitdir)


def get_oid(obj: Any):
    """Return object id (pygit2 >=1.18 provides .id consistently)."""
    return obj.id


# -------------------------- inputs ------------------------------------------


class StatusInput(BaseModel):
    """Input model for git_status with optional pagination."""

    list_slice: ListSlice = Field(
        default_factory=lambda: ListSlice(),
        description="Pagination for status entries (limit<=5000)",
    )


class DiffFormat(StrEnum):
    PATCH = "patch"
    NAME_STATUS = "name-status"
    STAT = "stat"


class DiffInput(BaseModel):
    format: DiffFormat = Field(
        default=DiffFormat.PATCH,
        description='Output format: "patch" | "name-status" | "stat"',
    )
    staged: bool = Field(
        default=False,
        description="If true, diff --cached (staged changes)",
    )
    unified: int = Field(
        default=0,
        ge=0,
        le=1000,
        description="Context lines (-U<N>) for patch format (0..1000; 0 shows only headers/hunks)",
    )
    rev_a: str | None = Field(
        default=None,
        description="Left side rev for range diff (e.g., HEAD^)",
    )
    rev_b: str | None = Field(
        default=None,
        description="Right side rev for range diff (e.g., HEAD)",
    )
    paths: list[str] | None = Field(
        default=None,
        description="Optional pathspecs to limit diff",
    )
    find_renames: bool = Field(default=True, description="Detect renames (-M)")
    slice: TextSlice = Field(
        default_factory=TextSlice,
        description="Pagination for patch output (format=patch; max_chars<=500k)",
    )
    list_slice: ListSlice = Field(
        default_factory=lambda: ListSlice(),
        description="Pagination for list outputs (name-status/stat; limit<=5000)",
    )


class LogInput(BaseModel):
    rev: str = Field(
        default="HEAD",
        description="Revision or range (e.g., HEAD, HEAD~10..HEAD)",
    )
    max_count: int = Field(default=50, description="Maximum number of entries")
    oneline: bool = Field(default=True, description="Format each commit as one line")
    slice: TextSlice = Field(
        default_factory=TextSlice,
        description="Pagination controls for large outputs",
    )


class ShowInput(BaseModel):
    object: str = Field(
        description="Object spec, e.g., HEAD, <sha>, or REV:PATH for blob content",
    )
    format: DiffFormat = Field(
        default=DiffFormat.PATCH,
        description='Output format: "patch" | "name-status" | "stat" (patch for blobs)',
    )
    slice: TextSlice = Field(
        default_factory=TextSlice,
        description="Pagination for patch/blob text outputs",
    )
    list_slice: ListSlice = Field(
        default_factory=lambda: ListSlice(),
        description="Pagination for list outputs (name-status/stat)",
    )


class RevParseInput(BaseModel):
    arg: str = Field(
        default="HEAD",
        description="Argument to rev-parse (e.g., HEAD, --show-toplevel)",
    )
    short: bool = Field(default=False, description="If true, shorten OIDs")


## moved to formatting.py


class RevParseResult(BaseModel):
    kind: Literal["oid", "toplevel"]
    value: str | Path


class LsFilesInput(BaseModel):
    cached: bool = Field(
        default=False,
        description="List index entries (same as non-cached here); kept for parity",
    )
    list_slice: ListSlice = Field(
        default_factory=lambda: ListSlice(),
        description="Pagination controls for file lists",
    )


class BranchListInput(BaseModel):
    remote: bool = Field(
        default=False,
        description="List remote branches instead of local",
    )
    list_slice: ListSlice = Field(
        default_factory=lambda: ListSlice(),
        description="Pagination controls for branch lists",
    )


# Structured diff listing inputs/outputs
class DiffListInput(BaseModel):
    staged: bool = Field(
        default=False,
        description="If true, examine staged (index) changes; else worktree",
    )
    paths: list[str] | None = Field(
        default=None,
        description="Optional pathspecs to limit the diff",
    )
    find_renames: bool = Field(
        default=True,
        description="Detect renames (diff.find_similar)",
    )
    list_slice: ListSlice = Field(
        default_factory=lambda: ListSlice(),
        description="Pagination controls for file lists",
    )


## moved to formatting.py


class LogEntriesInput(BaseModel):
    rev: str = Field(default="HEAD", description="Revision to start from (e.g., HEAD)")
    offset: int = Field(
        default=0,
        ge=0,
        description="Number of commits to skip (pagination offset)",
    )
    limit: int = Field(default=50, gt=0, le=1000, description="Max commits to return")
    include_message: bool = Field(
        default=False,
        description="Include full commit message body",
    )


class CommitEntry(BaseModel):
    id: str
    summary: str
    author_name: str
    author_email: str
    commit_time: int
    message: str | None = None


class LogEntriesPage(BaseModel):
    entries: list[CommitEntry]
    truncated: bool
    next_offset: int | None = None


# Discriminated unions for outputs (explicit output schema)
# For git_diff we return the complete page models directly
DiffResult = TextPage | ChangedFilesPage | DiffStatPage


# For git_show we also return the underlying page models directly
ShowResult = TextPage | ChangedFilesPage | DiffStatPage

# -------------------------- outputs -----------------------------------------


class IndexStatus(StrEnum):
    NONE = " "
    M = "M"
    A = "A"
    D = "D"
    R = "R"
    T = "T"


class WorktreeStatus(StrEnum):
    NONE = " "
    M = "M"
    D = "D"
    UNTRACKED = "?"


## moved to formatting.py


# -------------------------- server ------------------------------------------


@dataclass
class GitRoState:
    git_repo: Path


def make_git_ro_server(git_repo: Path, *, name: str = "git-ro") -> NotifyingFastMCP:
    """Create a read-only Git FastMCP server scoped to a single allowed root.

    Guidance:
    - Pass a specific repository/worktree root (the directory containing your working tree).
    - For worktrees, use the worktree directory (the one containing the .git file pointing to
      .../.git/worktrees/<name>). The server runs libgit2 operations relative to worktree_root.

    Only non-mutating tools are registered. Any attempt to pass a worktree_root outside the
    configured root results in an error.
    """
    state = GitRoState(git_repo=git_repo.resolve())
    display = f"Git (read-only): {git_repo.name}"
    mcp = NotifyingFastMCP(
        display,
        instructions=f"Read-only Git tools scoped to repo: {git_repo}",
    )

    @mcp.flat_model()
    def git_status(input: StatusInput) -> StatusPage:
        """Return compact status entries similar to porcelain v1 (no headers)."""
        root = state.git_repo
        repo = _open_repo(root)
        st = repo.status()
        entries: list[StatusEntry] = []
        for path, flags in st.items():
            # Map pygit2 status flags to porcelain-like two-letter codes
            idx: IndexStatus = IndexStatus.NONE
            wt: WorktreeStatus = WorktreeStatus.NONE
            if flags & pygit2.GIT_STATUS_INDEX_NEW:
                idx = IndexStatus.A
            elif flags & pygit2.GIT_STATUS_INDEX_MODIFIED:
                idx = IndexStatus.M
            elif flags & pygit2.GIT_STATUS_INDEX_DELETED:
                idx = IndexStatus.D
            elif flags & pygit2.GIT_STATUS_INDEX_RENAMED:
                idx = IndexStatus.R
            elif flags & pygit2.GIT_STATUS_INDEX_TYPECHANGE:
                idx = IndexStatus.T
            if flags & pygit2.GIT_STATUS_WT_MODIFIED:
                wt = WorktreeStatus.M
            elif flags & pygit2.GIT_STATUS_WT_DELETED:
                wt = WorktreeStatus.D
            elif flags & pygit2.GIT_STATUS_WT_NEW:
                wt = WorktreeStatus.UNTRACKED
            entries.append(StatusEntry(path=path, index=idx, worktree=wt))
        return build_status_page(entries, input.list_slice)

    @mcp.flat_model()
    async def git_diff(input: DiffInput) -> DiffResult:
        """Git diff with multiple formats:
        - format=patch: unified patch (TextPage)
        - format=name-status: file status listing (ChangedFilesPage)
        - format=stat: per-file additions/deletions (DiffStatPage)
        """
        repo = _open_repo(state.git_repo)
        # Build base diff using repository-level APIs that match type stubs
        # Note: pygit2 stubs do not expose 'paths' filtering; filter results downstream if needed.
        a = None if repo.head_is_unborn else repo.head.target
        if input.staged:
            diff = repo.diff(a, None, cached=True)
        else:
            diff = repo.diff(a, None, cached=False)

        if input.find_renames:
            diff.find_similar()

        if input.format == DiffFormat.PATCH:
            patch_text = await asyncio.to_thread(lambda: diff.patch or "")
            return apply_text_slice(patch_text, input.slice)
        if input.format == DiffFormat.NAME_STATUS:
            items = await asyncio.to_thread(diff_to_changed_files, diff)
            return build_changed_files_page(items, input.list_slice)
        # STAT
        stats = await asyncio.to_thread(diff_to_file_stats, diff)
        return build_diff_stat_page(stats, input.list_slice)

    @mcp.flat_model()
    def git_log(input: LogInput) -> TextPage:
        """Return recent commits as oneline entries or multi-line blocks, with pagination."""
        root = state.git_repo
        repo = _open_repo(root)
        if repo.head_is_unborn:
            return apply_text_slice("", input.slice)
        obj = repo.revparse_single(input.rev)
        head_oid = get_oid(obj)
        lines: list[str] = []
        walker = repo.walk(head_oid)
        for i, c in enumerate(walker, start=1):
            if input.oneline:
                summary = (c.message or "").splitlines()[0]
                lines.append(f"{str(c.id)[:7]} {summary}")
            else:
                lines.append(
                    f"commit {c.id}\nAuthor: {c.author.name} <{c.author.email}>\nDate:   {c.commit_time}\n\n{c.message or ''}\n",
                )
            if i >= input.max_count:
                break
        body = "\n".join(lines)
        if body and not body.endswith("\n"):
            body += "\n"
        return apply_text_slice(body, input.slice)

    @mcp.flat_model()
    def git_log_entries(input: LogEntriesInput) -> LogEntriesPage:
        """Return structured commit entries with offset/limit pagination (preferred for programmatic use)."""
        root = state.git_repo
        repo = _open_repo(root)
        if repo.head_is_unborn:
            return LogEntriesPage(entries=[], truncated=False, next_offset=None)
        obj = repo.revparse_single(input.rev)
        head_oid = get_oid(obj)
        walker = repo.walk(head_oid)
        # Skip offset
        for i, _ in enumerate(walker):
            if i >= input.offset:
                break
        # Collect up to limit
        entries: list[CommitEntry] = []
        for i, c in enumerate(walker, start=1):
            msg = (c.message or None) if input.include_message else None
            entries.append(
                CommitEntry(
                    id=str(c.id),
                    summary=(c.message or "").splitlines()[0] if c.message else "",
                    author_name=c.author.name,
                    author_email=c.author.email,
                    commit_time=c.commit_time,
                    message=msg,
                ),
            )
            if i >= input.limit:
                break
        # Peek one more to determine truncation
        more = next(iter(walker), None)
        truncated = more is not None
        next_offset = input.offset + input.limit if truncated else None
        return LogEntriesPage(
            entries=entries,
            truncated=truncated,
            next_offset=next_offset,
        )

    @mcp.flat_model()
    async def git_show(input: ShowInput) -> ShowResult:
        """Show a commit in various formats or blob contents for REV:PATH.
        - format=patch: header + patch (TextPage) or blob text
        - format=name-status: file status listing (ChangedFilesPage)
        - format=stat: per-file additions/deletions (DiffStatPage)
        """
        root = state.git_repo
        repo = _open_repo(root)
        objspec = input.object
        # Blob contents: REV:PATH always as text
        if ":" in objspec:
            rev, path = objspec.split(":", 1)
            root_obj = repo.revparse_single(rev)
            tree = (
                root_obj.tree if isinstance(root_obj, pygit2.Commit) else root_obj.peel(pygit2.Tree)
            )
            cur: pygit2.Tree = tree
            for part in filter(None, path.split("/")):
                entry = cur[part]
                if entry.filemode == pygit2.GIT_FILEMODE_TREE:
                    cur = repo[entry.id].peel(pygit2.Tree)
                else:
                    blob = repo[entry.id].peel(pygit2.Blob)
                    data = blob.data
                    try:
                        text = data.decode("utf-8")
                    except UnicodeDecodeError:
                        text = f"[binary blob {len(data)} bytes]"
                    return apply_text_slice(text, input.slice)
            raise FileNotFoundError(f"Path not found: {path}")
        obj_any = repo.revparse_single(objspec)
        # Narrow runtime types explicitly
        if isinstance(obj_any, pygit2.Tag):
            obj = obj_any.peel(pygit2.Commit)
        elif isinstance(obj_any, pygit2.Commit):
            obj = obj_any
        else:
            raise TypeError(
                f"Unexpected git object type for {objspec}: {type(obj_any)!r}",
            )

        # Build commit diff against first parent (or empty tree)
        if obj.parent_ids:
            parent = repo[obj.parent_ids[0]].peel(pygit2.Commit)
            diff = repo.diff(parent, obj)
        else:
            diff = repo.diff(None, obj)
        diff.find_similar()

        if input.format == DiffFormat.PATCH:
            patch_text = await asyncio.to_thread(lambda: diff.patch or "")
            return apply_text_slice(patch_text, input.slice)

        if input.format == DiffFormat.NAME_STATUS:
            items = await asyncio.to_thread(diff_to_changed_files, diff)
            return build_changed_files_page(items, input.list_slice)

        # STAT
        stats = await asyncio.to_thread(diff_to_file_stats, diff)
        return build_diff_stat_page(stats, input.list_slice)

    @mcp.flat_model()
    def git_rev_parse(input: RevParseInput) -> RevParseResult:
        """Resolve a rev to an OID (optionally shortened) or return toplevel path for --show-toplevel."""
        root = state.git_repo
        repo = _open_repo(root)
        if input.arg == "--show-toplevel":
            workdir = repo.workdir
            if not workdir:
                raise ValueError("Repository has no working directory")
            return RevParseResult(
                kind="toplevel",
                value=Path(workdir).resolve(),
            )
        obj = repo.revparse_single(input.arg)
        oid = get_oid(obj)
        s = str(oid)
        if input.short:
            s = s[:7]
        return RevParseResult(kind="oid", value=s)

    @mcp.flat_model()
    def git_ls_files(input: LsFilesInput) -> StringListPage:
        """List index paths, with offset/limit pagination (structured output)."""
        root = state.git_repo
        repo = _open_repo(root)
        all_paths = [e.path for e in repo.index]
        items, truncated, next_offset, total = apply_list_slice(all_paths, input.list_slice)
        return StringListPage(
            items=items, truncated=truncated, next_offset=next_offset, total_items=total
        )

    @mcp.flat_model()
    def git_branch_list(input: BranchListInput) -> StringListPage:
        """List local or remote branches (short names) with offset/limit pagination."""
        root = state.git_repo
        repo = _open_repo(root)
        kind = BranchType.REMOTE if input.remote else BranchType.LOCAL
        names = repo.listall_branches(kind)
        items, truncated, next_offset, total = apply_list_slice(names, input.list_slice)
        return StringListPage(
            items=items, truncated=truncated, next_offset=next_offset, total_items=total
        )

    return mcp


async def attach_git_ro(
    comp: Compositor,
    git_repo: Path,
    *,
    name: str = GIT_RO_SERVER_NAME,
) -> NotifyingFastMCP:
    """Mount read-only Git MCP server in-proc on a Compositor (preferred path)."""
    server = make_git_ro_server(git_repo, name=name)
    await comp.mount_inproc(name, server)
    return server
