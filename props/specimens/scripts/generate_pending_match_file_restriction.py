"""Generate markdown listing TP/FP occurrences missing match_file_restriction.

Uses props' existing Pydantic models (YAMLIssue, YAMLOccurrence, LineRange)
for parsing. Produces verifiable output with GitHub links for each occurrence.

For detailed per-occurrence analysis with validation proofs, use the
narrow_matchability skill: `/narrow_matchability <snapshot_slug>`
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from props.core.models.true_positive import LineRange
from props.db.sync.yaml_loader import YAMLIssue, YAMLOccurrence
from util.bazel.workspace import get_build_workspace_directory

# ---------------------------------------------------------------------------
# GitHub link construction
# ---------------------------------------------------------------------------

GITHUB_BASE = "https://github.com/agentydragon"
REPO_BRANCH = "devel"


@dataclass(frozen=True)
class SpecimenSource:
    """GitHub source info for a specimen snapshot."""

    github_org: str
    github_repo: str
    commit: str
    local_code: bool  # True = code/ dir in-repo, False = external archive


# Mapping from snapshot slug prefix to source info.
# External specimens link to original repo at commit SHA.
# Local specimens link to committed code in the ducktape repo.
SPECIMEN_SOURCES: dict[str, SpecimenSource] = {
    "crush/2025-08-30-internal_db": SpecimenSource(
        github_org="agentydragon",
        github_repo="crush",
        commit="a2a1ffa00943aa373f688ac05b667083ac3230b1",
        local_code=False,
    ),
    "ducktape/2025-09-03-00": SpecimenSource(
        github_org="agentydragon",
        github_repo="ducktape",
        commit="4ad33013af27e159863bed92ffcfdb55b388e46c",
        local_code=False,
    ),
    "ducktape/2025-11-20-00": SpecimenSource(
        github_org="agentydragon", github_repo="ducktape", commit="b729b362", local_code=True
    ),
    "ducktape/2025-11-20-01": SpecimenSource(
        github_org="agentydragon", github_repo="ducktape", commit="c18279d2", local_code=True
    ),
    "ducktape/2025-11-21-00": SpecimenSource(
        github_org="agentydragon", github_repo="ducktape", commit="167e3901", local_code=True
    ),
    "ducktape/2025-11-22-00": SpecimenSource(
        github_org="agentydragon", github_repo="ducktape", commit="9395ba65", local_code=True
    ),
    "ducktape/2025-11-22-01": SpecimenSource(
        github_org="agentydragon", github_repo="ducktape", commit="c263d680", local_code=True
    ),
    "ducktape/2025-11-22-02": SpecimenSource(
        github_org="agentydragon", github_repo="ducktape", commit="a81550a1", local_code=True
    ),
    "ducktape/2025-11-26-00": SpecimenSource(
        github_org="agentydragon", github_repo="ducktape", commit="751a2a33", local_code=True
    ),
    "ducktape/2025-12-04-00": SpecimenSource(
        github_org="agentydragon", github_repo="ducktape", commit="ab7e9d6f", local_code=True
    ),
    "ducktape/2026-01-17-00": SpecimenSource(
        github_org="agentydragon", github_repo="ducktape", commit="ed0b4c0a", local_code=True
    ),
    "ducktape/2026-01-29-00": SpecimenSource(
        github_org="agentydragon", github_repo="ducktape", commit="d1ab8dd7", local_code=True
    ),
    "ducktape_llm_common/2026-01-03-00": SpecimenSource(
        github_org="agentydragon", github_repo="ducktape", commit="7cfd2bb3", local_code=True
    ),
    "gmail-archiver/2025-12-17-00": SpecimenSource(
        github_org="agentydragon", github_repo="ducktape", commit="", local_code=True
    ),
    "misc/2025-08-29-pyright_watch_report": SpecimenSource(
        github_org="agentydragon", github_repo="ducktape", commit="", local_code=True
    ),
    "wt/2025-01-03-00": SpecimenSource(
        github_org="agentydragon", github_repo="ducktape", commit="7cfd2bb3", local_code=True
    ),
}


def github_file_link(slug: str, file_path: str, ranges: list[LineRange] | None) -> str | None:
    """Build a GitHub link to a file in a specimen, with optional line range fragment."""
    source = SPECIMEN_SOURCES.get(slug)
    if source is None:
        return None

    line_fragment = ""
    if ranges:
        start = min(r.start_line for r in ranges)
        end = max(r.end_line or r.start_line for r in ranges)
        line_fragment = f"#L{start}-L{end}" if start != end else f"#L{start}"

    if source.local_code:
        # Link to committed specimen code in the ducktape repo
        return f"{GITHUB_BASE}/ducktape/blob/{REPO_BRANCH}/props/specimens/{slug}/code/{file_path}{line_fragment}"
    # Link to original repo at the specimen commit
    return f"{GITHUB_BASE}/{source.github_repo}/blob/{source.commit}/{file_path}{line_fragment}"


def github_yaml_link(slug: str, issue_filename: str) -> str:
    """Build a GitHub link to the issue YAML file."""
    return f"{GITHUB_BASE}/ducktape/blob/{REPO_BRANCH}/props/specimens/{slug}/issues/{issue_filename}"


# ---------------------------------------------------------------------------
# Pending occurrence model
# ---------------------------------------------------------------------------


@dataclass
class PendingOccurrence:
    """A single TP occurrence missing match_file_restriction."""

    slug: str
    issue_filename: str
    issue: YAMLIssue
    occurrence: YAMLOccurrence
    issue_path: Path

    @property
    def occurrence_id(self) -> str:
        return self.occurrence.occurrence_id

    @property
    def file_paths(self) -> list[str]:
        return list(self.occurrence.files.keys())

    @property
    def is_single_file(self) -> bool:
        return len(self.occurrence.files) == 1

    @property
    def single_file(self) -> str:
        """The single file path (only valid when is_single_file is True)."""
        return next(iter(self.occurrence.files.keys()))

    @property
    def single_file_ranges(self) -> list[LineRange] | None:
        """Line ranges for the single file (only valid when is_single_file is True)."""
        return next(iter(self.occurrence.files.values()))


# ---------------------------------------------------------------------------
# Difficulty classification
# ---------------------------------------------------------------------------


class Difficulty:
    """Classification of how easy it is to verify an occurrence is single-file matchable."""

    EASY_SINGLE_FILE = 10  # Obviously local to one file, no cross-file implications
    EASY_SAME_FILE_GROUP = 15  # Multiple occurrences all in same file
    MEDIUM_EACH_OWN_FILE = 20  # Each occurrence in its own file, straightforward
    UNCLASSIFIED = 1000  # Not yet manually reviewed
    SKIP_CROSS_FILE = 9999  # Cross-file implications, should NOT set field


# Priority ordering: easiest to confirm -> hardest
# Format: "snapshot_slug|issue_file|occ_id" -> priority
PRIORITY_ORDER: dict[str, int] = {
    # === EASY (P10): Obviously single-file local issues ===
    "crush/2025-08-30-internal_db|bash-timeout-docs-mismatch.yaml|occ-0": Difficulty.EASY_SINGLE_FILE,
    "crush/2025-08-30-internal_db|glob-sort-docs-mismatch.yaml|occ-0": Difficulty.EASY_SINGLE_FILE,
    "crush/2025-08-30-internal_db|misleading-func-name.yaml|occ-0": Difficulty.EASY_SINGLE_FILE,
    "crush/2025-08-30-internal_db|sentinel-flag-pattern.yaml|occ-0": Difficulty.EASY_SINGLE_FILE,
    "crush/2025-08-30-internal_db|lsp-stdin-race.yaml|occ-0": Difficulty.EASY_SINGLE_FILE,
    "crush/2025-08-30-internal_db|create-replace-fallthrough.yaml|occ-0": Difficulty.EASY_SINGLE_FILE,
    # === EASY (P15): All occurrences in same file, easy bulk assignment ===
    **{
        f"crush/2025-08-30-internal_db|renderer-guard-clauses.yaml|occ-{i}": Difficulty.EASY_SAME_FILE_GROUP
        for i in range(9)
    },
    "ducktape/2025-11-26-00|agentiddata-wrapper-class.yaml|occ-0": Difficulty.EASY_SAME_FILE_GROUP,
    "ducktape/2025-11-26-00|common-diff-trunk.yaml|occ-0": Difficulty.EASY_SAME_FILE_GROUP,
    "ducktape/2025-11-26-00|delete-install-wrapper.yaml|occ-0": Difficulty.EASY_SAME_FILE_GROUP,
    # === MEDIUM (P20): Each occurrence in its own file, straightforward ===
    **{
        f"crush/2025-08-30-internal_db|config-nil-chains.yaml|occ-{i}": Difficulty.MEDIUM_EACH_OWN_FILE
        for i in range(3)
    },
    **{
        f"crush/2025-08-30-internal_db|control-flow-complexity.yaml|occ-{i}": Difficulty.MEDIUM_EACH_OWN_FILE
        for i in range(7)
    },
    **{
        f"crush/2025-08-30-internal_db|hardcoded-timeouts.yaml|occ-{i}": Difficulty.MEDIUM_EACH_OWN_FILE
        for i in range(9)
    },
    **{
        f"crush/2025-08-30-internal_db|timestamp-type-inconsistency.yaml|occ-{i}": Difficulty.MEDIUM_EACH_OWN_FILE
        for i in range(11)
    },
    **{
        f"crush/2025-08-30-internal_db|path-schema-docs-mismatch.yaml|occ-{i}": Difficulty.MEDIUM_EACH_OWN_FILE
        for i in range(2)
    },
    "ducktape/2025-11-26-00|agent-id-fields-use-str.yaml|occ-0": Difficulty.MEDIUM_EACH_OWN_FILE,
    "ducktape/2025-11-26-00|allow-case-two-ids.yaml|occ-0": Difficulty.MEDIUM_EACH_OWN_FILE,
    "ducktape/2025-11-26-00|ask-approved-inflight.yaml|occ-0": Difficulty.MEDIUM_EACH_OWN_FILE,
    "ducktape/2025-11-20-00|collection-params-empty-tuple.yaml|occ-2": Difficulty.MEDIUM_EACH_OWN_FILE,
    "ducktape/2025-11-20-00|collection-params-empty-tuple.yaml|occ-3": Difficulty.MEDIUM_EACH_OWN_FILE,
    "ducktape/2025-11-20-00|proposal-id-type-mismatch.yaml|occ-0": Difficulty.MEDIUM_EACH_OWN_FILE,
    "ducktape/2025-11-20-00|proposal-id-type-mismatch.yaml|occ-1": Difficulty.MEDIUM_EACH_OWN_FILE,
    "ducktape/2025-11-20-00|proposal-id-type-mismatch.yaml|occ-2": Difficulty.MEDIUM_EACH_OWN_FILE,
}


def get_priority(occ: PendingOccurrence) -> int:
    """Get priority for an occurrence. Lower = easier = show first."""
    key = f"{occ.slug}|{occ.issue_filename}|{occ.occurrence_id}"
    return PRIORITY_ORDER.get(key, Difficulty.UNCLASSIFIED)


def priority_label(priority: int) -> str:
    if priority == Difficulty.SKIP_CROSS_FILE:
        return " [SKIP - cross-file]"
    if priority == Difficulty.UNCLASSIFIED:
        return ""
    return f" [P{priority}]"


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------


@dataclass
class ScanResult:
    """Result of scanning specimens for pending occurrences."""

    pending: list[PendingOccurrence] = field(default_factory=list)
    by_snapshot: dict[str, list[PendingOccurrence]] = field(default_factory=dict)

    def group_by_snapshot(self) -> None:
        self.by_snapshot.clear()
        for occ in self.pending:
            self.by_snapshot.setdefault(occ.slug, []).append(occ)


def scan_specimens(specimens_root: Path) -> ScanResult:
    """Scan all specimens for TP occurrences missing match_file_restriction."""
    result = ScanResult()

    for issue_path in sorted(specimens_root.rglob("issues/*.yaml")):
        # Skip testdata fixtures nested inside specimen code/ directories
        rel = issue_path.relative_to(specimens_root)
        if "code" in rel.parts:
            continue
        # Skip pyright_watch_report (single-file snapshot)
        if "pyright_watch_report" in str(issue_path):
            continue

        with issue_path.open() as f:
            raw = yaml.safe_load(f)
        if not raw:
            continue

        issue = YAMLIssue.model_validate(raw)

        if not issue.should_flag:
            continue

        snapshot_dir = issue_path.parent.parent
        project = snapshot_dir.parent.name
        snapshot_name = snapshot_dir.name
        slug = f"{project}/{snapshot_name}"

        for occ in issue.occurrences:
            if occ.match_file_restriction is not None:
                continue
            if not _is_single_file(occ):
                continue

            result.pending.append(
                PendingOccurrence(
                    slug=slug, issue_filename=issue_path.name, issue=issue, occurrence=occ, issue_path=issue_path
                )
            )

    result.group_by_snapshot()
    return result


def _is_single_file(occ: YAMLOccurrence) -> bool:
    return len(occ.files) == 1


# ---------------------------------------------------------------------------
# Markdown generation
# ---------------------------------------------------------------------------


def _format_line_ranges(ranges: list[LineRange] | None) -> str:
    """Format line ranges for display."""
    if not ranges:
        return "(whole file)"
    parts = []
    for r in ranges:
        if r.end_line is None or r.start_line == r.end_line:
            parts.append(f"L{r.start_line}")
        else:
            parts.append(f"L{r.start_line}-{r.end_line}")
    return ", ".join(parts)


CONTEXT_LINES = 3


def _render_code_context(occ: PendingOccurrence, specimens_root: Path) -> list[str]:
    """Render code context snippet with merged overlapping context windows.

    When multiple line ranges are close enough that their +/- context windows
    overlap, renders them as a single continuous block with >>> markers on
    highlighted lines, avoiding duplicated context.
    """
    lines: list[str] = []
    snapshot_dir = occ.issue_path.parent.parent
    file_path_str = occ.single_file

    # Check for code/ subdirectory (vcs: local with root: code)
    source_file = snapshot_dir / "code" / file_path_str
    if not source_file.exists():
        source_file = snapshot_dir / file_path_str
    if not source_file.exists():
        return lines

    ranges = occ.single_file_ranges
    if not ranges:
        return lines

    source_lines = source_file.read_text().splitlines()
    total_lines = len(source_lines)

    # Build set of highlighted line numbers
    highlighted: set[int] = set()
    for lr in ranges:
        end = lr.end_line or lr.start_line
        for i in range(lr.start_line, end + 1):
            highlighted.add(i)

    # Build merged display intervals (context windows that overlap are merged)
    intervals: list[tuple[int, int]] = []
    for lr in sorted(ranges, key=lambda r: r.start_line):
        start = lr.start_line
        end = lr.end_line or lr.start_line
        ctx_start = max(1, start - CONTEXT_LINES)
        ctx_end = min(total_lines, end + CONTEXT_LINES)
        if intervals and ctx_start <= intervals[-1][1] + 1:
            # Overlaps or adjacent — extend previous interval
            intervals[-1] = (intervals[-1][0], max(intervals[-1][1], ctx_end))
        else:
            intervals.append((ctx_start, ctx_end))

    lines.append("```")
    for idx, (iv_start, iv_end) in enumerate(intervals):
        for i in range(iv_start, iv_end + 1):
            if i <= total_lines:
                marker = ">>>" if i in highlighted else "   "
                lines.append(f"{marker} {i:4d}: {source_lines[i - 1]}")
        if idx < len(intervals) - 1:
            lines.append("   ...")
    lines.append("```")
    return lines


def generate_markdown(result: ScanResult, specimens_root: Path) -> str:
    """Generate the full markdown report."""
    lines: list[str] = [
        "# Issues Missing `match_file_restriction`",
        "",
        f"Total: {len(result.pending)} single-file TP occurrences without `match_file_restriction`.",
        "",
        "For detailed per-occurrence analysis with validation proofs, use:",
        "`/narrow_matchability <snapshot_slug>`",
        "",
    ]

    # Sort snapshots by descending count
    for slug in sorted(result.by_snapshot, key=lambda k: -len(result.by_snapshot[k])):
        snapshot_occs = result.by_snapshot[slug]
        lines.append(f"## {slug} ({len(snapshot_occs)})")
        lines.append("")

        # Sort by priority then issue file then occurrence id
        for occ in sorted(snapshot_occs, key=lambda o: (get_priority(o), o.issue_filename, o.occurrence_id)):
            priority = get_priority(occ)
            label = priority_label(priority)
            file_path_str = occ.single_file
            ranges = occ.single_file_ranges

            yaml_link = github_yaml_link(occ.slug, occ.issue_filename)
            code_link = github_file_link(occ.slug, file_path_str, ranges)

            lines.append(f"### `{occ.issue_filename}` / `{occ.occurrence_id}`{label}")

            # Links line
            link_parts = [f"[YAML]({yaml_link})"]
            if code_link:
                link_parts.append(f"[code]({code_link})")
            lines.append(f"Links: {' · '.join(link_parts)}")
            lines.append("")

            lines.append(f"File: `{file_path_str}` ({_format_line_ranges(ranges)})")

            # Rationale as blockquote
            rationale = occ.issue.rationale.strip()
            if rationale:
                lines.append("")
                lines.extend(f"> {rationale_line}" for rationale_line in rationale.split("\n"))

            # Occurrence-level note
            if occ.occurrence.note:
                lines.append(">")
                lines.append(f"> **Note:** {occ.occurrence.note}")

            # Code context
            code_lines = _render_code_context(occ, specimens_root)
            if code_lines:
                lines.append("")
                lines.extend(code_lines)

            lines.append("")

        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _resolve_specimens_root() -> Path:
    """Resolve the specimens root directory.

    Uses bazel_util to find the workspace root (handles both `bazel run` and direct invocation).
    """
    return get_build_workspace_directory() / "props" / "specimens"


def main() -> None:
    specimens_root = _resolve_specimens_root()
    result = scan_specimens(specimens_root)

    markdown = generate_markdown(result, specimens_root)

    output_path = specimens_root / "pending-match-file-restriction.md"
    output_path.write_text(markdown)
    print(f"Wrote {len(result.pending)} occurrences to {output_path}")


if __name__ == "__main__":
    main()
