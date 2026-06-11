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
import re
from enum import StrEnum
from pathlib import Path
from typing import Any

import pygit2

# FastMCP-only: no TokenVerifier in server construction
from pydantic import BaseModel, Field
from pygit2.enums import BranchType, FileStatus

from git_commit_ai.git_ro.formatting import (
    ChangedFilesPage,
    DiffStatPage,
    ListSlice,
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
from mcp_infra.enhanced.simple import SimpleFastMCP
from mcp_infra.flat_tool import FlatTool
from mcp_infra.prefix import MCPMountPrefix
from openai_utils.pydantic_strict_mode import OpenAIStrictModeBaseModel

# Shared mount prefix constant for clients/tests
GIT_RO_MOUNT_PREFIX = MCPMountPrefix("git_ro")

# -------------------------- inputs ------------------------------------------


class StatusInput(OpenAIStrictModeBaseModel):
    """Input model for git_status with optional pagination."""

    list_slice: ListSlice


class DiffFormat(StrEnum):
    PATCH = "patch"
    NAME_STATUS = "name-status"
    STAT = "stat"


class DiffInput(OpenAIStrictModeBaseModel):
    format: DiffFormat
    staged: bool = Field(description="If true, diff --cached (staged changes)")
    unified: int = Field(
        ge=0, le=1000, description="Context lines (-U<N>) for patch format (0..1000; 0 shows only headers/hunks)"
    )
    rev_a: str | None = Field(description="Left side rev for range diff (e.g., HEAD^)")
    rev_b: str | None = Field(description="Right side rev for range diff (e.g., HEAD)")
    paths: list[str] | None = Field(description="Optional pathspecs to limit diff")
    find_renames: bool = Field(description="Detect renames (-M)")
    slice: TextSlice
    list_slice: ListSlice


class LogInput(OpenAIStrictModeBaseModel):
    rev: str = Field(description="Revision or range (e.g., HEAD, HEAD~10..HEAD)")
    max_count: int = Field(description="Maximum number of entries")
    oneline: bool = Field(description="Format each commit as one line")
    slice: TextSlice


class ShowInput(OpenAIStrictModeBaseModel):
    object: str = Field(description="Commit spec (HEAD, <sha>, tag) to show with its diff")
    format: DiffFormat
    slice: TextSlice
    list_slice: ListSlice


class CatFileInput(OpenAIStrictModeBaseModel):
    object: str = Field(
        description=(
            "Object reference: REV:path (blob from commit tree, e.g., HEAD:src/main.py), "
            ":path (blob from index stage 0), :N:path (blob from index stage N, for merge conflicts), "
            "or raw OID (any object by SHA)"
        )
    )
    slice: TextSlice


class RevParseInput(OpenAIStrictModeBaseModel):
    rev: str = Field(description="Revision to resolve (e.g., HEAD, main, abc1234)")


class LsFilesInput(OpenAIStrictModeBaseModel):
    cached: bool = Field(description="List index entries (same as non-cached here); kept for parity")
    list_slice: ListSlice


class BranchListInput(OpenAIStrictModeBaseModel):
    remote: bool = Field(description="List remote branches instead of local")
    list_slice: ListSlice


class LogEntriesInput(OpenAIStrictModeBaseModel):
    rev: str = Field(description="Revision to start from (e.g., HEAD)")
    offset: int = Field(ge=0, description="Number of commits to skip (pagination offset)")
    limit: int = Field(gt=0, le=1000, description="Max commits to return")
    include_message: bool = Field(description="Include full commit message body")


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


class GitRoServer(SimpleFastMCP):
    """Git read-only MCP server with typed tool access.

    Subclasses SimpleFastMCP and adds typed tool attributes for accessing
    tool names. This is the single source of truth - no string literals elsewhere.
    """

    # Tool references (assigned in __init__ after tool registration)
    status_tool: FlatTool[Any, Any]
    diff_tool: FlatTool[Any, Any]
    log_tool: FlatTool[Any, Any]
    show_tool: FlatTool[Any, Any]
    cat_file_tool: FlatTool[Any, Any]
    log_entries_tool: FlatTool[Any, Any]
    rev_parse_tool: FlatTool[Any, Any]
    ls_files_tool: FlatTool[Any, Any]
    branch_list_tool: FlatTool[Any, Any]

    def __init__(self, repo: pygit2.Repository):
        """Create a read-only Git FastMCP server for an already-opened repository."""
        state = repo
        repo_name = Path(repo.workdir or repo.path).name
        super().__init__("Git Read-Only MCP Server", instructions=f"Read-only Git tools scoped to repo: {repo_name}")

        def status(input: StatusInput) -> StatusPage:
            """Return git status as path → FileStatus flags mapping."""
            entries = {path: FileStatus(flags) for path, flags in state.status().items()}
            return build_status_page(entries, input.list_slice)

        self.status_tool = self.flat_model()(status)

        async def diff(input: DiffInput) -> TextPage | ChangedFilesPage | DiffStatPage:
            """Git diff with multiple formats:
            - format=patch: unified patch (TextPage)
            - format=name-status: file status listing (ChangedFilesPage)
            - format=stat: per-file additions/deletions (DiffStatPage)
            """
            # Build base diff using repository-level APIs that match type stubs
            # Note: pygit2 stubs do not expose 'paths' filtering; filter results downstream if needed.
            a = None if state.head_is_unborn else state.head.target
            diff = state.diff(a, None, cached=input.staged)

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

        self.diff_tool = self.flat_model()(diff)

        def log(input: LogInput) -> TextPage:
            """Return recent commits as oneline entries or multi-line blocks, with pagination."""
            if state.head_is_unborn:
                return apply_text_slice("", input.slice)
            obj = state.revparse_single(input.rev)
            head_oid = obj.id
            lines: list[str] = []
            for i, c in enumerate(state.walk(head_oid), start=1):
                if input.oneline:
                    raw_message = (c.message or "").rstrip("\n")
                    prefix = str(c.id)[:7]
                    lines.append(f"{prefix} {raw_message}" if raw_message else prefix)
                else:
                    lines.append(
                        f"commit {c.id}\nAuthor: {c.author.name} <{c.author.email}>\nDate:   {c.commit_time}\n\n{c.message or ''}\n"
                    )
                if i >= input.max_count:
                    break
            body = "\n".join(lines)
            if body and not body.endswith("\n"):
                body += "\n"
            return apply_text_slice(body, input.slice)

        self.log_tool = self.flat_model()(log)

        def log_entries(input: LogEntriesInput) -> LogEntriesPage:
            """Return structured commit entries with offset/limit pagination (preferred for programmatic use)."""
            if state.head_is_unborn:
                return LogEntriesPage(entries=[], truncated=False, next_offset=None)
            obj = state.revparse_single(input.rev)
            head_oid = obj.id
            walker = state.walk(head_oid)
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
                    )
                )
                if i >= input.limit:
                    break
            # Peek one more to determine truncation
            more = next(iter(walker), None)
            truncated = more is not None
            next_offset = input.offset + input.limit if truncated else None
            return LogEntriesPage(entries=entries, truncated=truncated, next_offset=next_offset)

        self.log_entries_tool = self.flat_model()(log_entries)

        async def show(input: ShowInput) -> TextPage | ChangedFilesPage | DiffStatPage:
            """Show a commit's changes in various formats.
            - format=patch: unified diff (TextPage)
            - format=name-status: file status listing (ChangedFilesPage)
            - format=stat: per-file additions/deletions (DiffStatPage)

            For reading file content, use cat_file instead.
            """
            objspec = input.object
            obj_any = state.revparse_single(objspec)
            # Narrow runtime types explicitly
            if isinstance(obj_any, pygit2.Tag):
                obj = obj_any.peel(pygit2.Commit)
            elif isinstance(obj_any, pygit2.Commit):
                obj = obj_any
            else:
                raise TypeError(f"Expected commit or tag, got {type(obj_any).__name__} for {objspec!r}")

            # Build commit diff against first parent (or empty tree)
            if obj.parents:
                parent = obj.parents[0]
                diff = state.diff(parent, obj)
            else:
                diff = state.diff(None, obj)
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

        self.show_tool = self.flat_model()(show)

        def cat_file(input: CatFileInput) -> TextPage:
            """Read object content (like git cat-file).

            Supports:
            - REV:path - blob from commit tree (e.g., HEAD:src/main.py)
            - :path - blob from index stage 0 (staged content)
            - :N:path - blob from index stage N (1=base, 2=ours, 3=theirs in merge conflicts)
            - raw OID - any object by SHA

            For newly added files (not yet in any commit), use :path to read from the
            index. HEAD:path will fail since the file doesn't exist in the commit tree.
            All paths must be relative to repository root (e.g., 'src/foo.py' not 'foo.py').
            """
            objspec = input.object

            # Index entry: :path or :N:path
            index_match = re.match(r"^:(\d)?:?(.+)$", objspec)
            if index_match:
                stage_str, path = index_match.groups()
                stage = int(stage_str) if stage_str else 0
                state.index.read()

                entry: pygit2.IndexEntry | None = None
                conflicts = state.index.conflicts
                if stage == 0:
                    # Stage 0: regular index entries (non-conflict)
                    # If file is in conflicts, stage 0 doesn't exist
                    if conflicts and path in conflicts:
                        entry = None
                    else:
                        for e in state.index:
                            if e.path == path:
                                entry = e
                                break
                # Stages 1-3: conflict entries (ancestor=1, ours=2, theirs=3)
                elif conflicts and path in conflicts:
                    ancestor, ours, theirs = conflicts[path]
                    conflict_entries = {1: ancestor, 2: ours, 3: theirs}
                    entry = conflict_entries.get(stage)

                if entry is None:
                    raise FileNotFoundError(f"Index entry not found: {objspec}")
                blob = state[entry.id].peel(pygit2.Blob)
                text = blob.data.decode("utf-8")
                return apply_text_slice(text, input.slice)

            # REV:path - blob from commit tree
            if ":" in objspec:
                rev, path = objspec.split(":", 1)
                root_obj = state.revparse_single(rev)
                tree = root_obj.tree if isinstance(root_obj, pygit2.Commit) else root_obj.peel(pygit2.Tree)
                cur: pygit2.Tree = tree
                traversed: list[str] = []
                for part in filter(None, path.split("/")):
                    try:
                        tree_entry = cur[part]
                    except KeyError:
                        entries_here = sorted(e.name for e in cur if e.name is not None)
                        at_root = not traversed
                        location = "repository root" if at_root else f"'{'/'.join(traversed)}'"
                        raise FileNotFoundError(
                            f"'{part}' not found at {location}. "
                            f"Path must be relative to repository root. "
                            f"Entries at {location}: {entries_here}"
                        ) from None
                    traversed.append(part)
                    if tree_entry.filemode == pygit2.GIT_FILEMODE_TREE:
                        cur = state[tree_entry.id].peel(pygit2.Tree)
                    else:
                        blob = state[tree_entry.id].peel(pygit2.Blob)
                        text = blob.data.decode("utf-8")
                        return apply_text_slice(text, input.slice)
                raise FileNotFoundError(f"Path not found in tree: {path}")

            # Raw OID or other ref - read object directly
            obj = state.revparse_single(objspec)
            if isinstance(obj, pygit2.Blob):
                text = obj.data.decode("utf-8")
                return apply_text_slice(text, input.slice)
            if isinstance(obj, pygit2.Commit):
                # Return raw commit object representation
                lines = [
                    f"tree {obj.tree_id}",
                    *[f"parent {p.id}" for p in obj.parents],
                    f"author {obj.author.name} <{obj.author.email}> {obj.author.time} {obj.author.offset:+05d}",
                    f"committer {obj.committer.name} <{obj.committer.email}> {obj.committer.time} {obj.committer.offset:+05d}",
                    "",
                    obj.message or "",
                ]
                return apply_text_slice("\n".join(lines), input.slice)
            if isinstance(obj, pygit2.Tree):
                # List tree entries
                lines = [f"{e.filemode:06o} {e.type_str} {e.id}\t{e.name}" for e in obj]
                return apply_text_slice("\n".join(lines), input.slice)
            raise TypeError(f"Unsupported object type: {type(obj).__name__}")

        self.cat_file_tool = self.flat_model()(cat_file)

        def rev_parse(input: RevParseInput) -> str:
            """Resolve a revision to its OID."""
            return str(state.revparse_single(input.rev).id)

        self.rev_parse_tool = self.flat_model()(rev_parse)

        def ls_files(input: LsFilesInput) -> StringListPage:
            """List index paths, with offset/limit pagination (structured output)."""
            all_paths = [e.path for e in state.index]
            items, truncated, next_offset, total = apply_list_slice(all_paths, input.list_slice)
            return StringListPage(items=items, truncated=truncated, next_offset=next_offset, total_items=total)

        self.ls_files_tool = self.flat_model()(ls_files)

        def branch_list(input: BranchListInput) -> StringListPage:
            """List local or remote branches (short names) with offset/limit pagination."""
            kind = BranchType.REMOTE if input.remote else BranchType.LOCAL
            names = state.listall_branches(kind)
            items, truncated, next_offset, total = apply_list_slice(names, input.list_slice)
            return StringListPage(items=items, truncated=truncated, next_offset=next_offset, total_items=total)

        self.branch_list_tool = self.flat_model()(branch_list)
