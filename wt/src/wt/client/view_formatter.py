import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlunparse

import click
from colorama import Style
from tabulate import tabulate

from ..shared.github_models import PRData, PRState, PRStatus
from ..shared.protocol import PRInfo, PRInfoError, PRInfoOk, StatusResult

# PR status display mapping centralized via PRStatus.display_text
PR_STATUS_DISPLAY_MAP = {
    PRStatus.MERGED.display_text: ("✅", "already merged"),
    PRStatus.CLOSED.display_text: ("❌", "closed"),
    PRStatus.OPEN_MERGEABLE.display_text: ("🟢", "can merge"),
    PRStatus.OPEN_CONFLICTING.display_text: ("🔴", "has conflict"),
    PRStatus.OPEN_UNKNOWN.display_text: ("🟡", "open"),
}


def format_sync_status(ahead: int, behind: int, *, compact: bool = False) -> str:
    """Format ahead/behind status.

    - compact=False (default): fixed-width aligned display for rows
    - compact=True: short form like "↓3 ↑2" or "" when zero
    """
    if compact:
        parts: list[str] = []
        if behind > 0:
            parts.append(f"↓{behind}")
        if ahead > 0:
            parts.append(f"↑{ahead}")
        return " ".join(parts)

    if ahead == 0 and behind == 0:
        return "          "  # Fixed width for alignment

    left = f"{behind:>4}↓" if behind > 0 else "     "
    right = f"↑{ahead:<4}" if ahead > 0 else "     "
    content = f"{left} {right}"

    return f"{Style.DIM}{content}{Style.RESET_ALL}"


class ViewFormatter:
    def __init__(self, daemon_log_path: Path | None = None):
        self.daemon_log_path = daemon_log_path

    def make_hyperlink(self, url: str, text: str) -> str:
        if os.getenv("TERM_PROGRAM") in ("iTerm.app", "vscode") or os.getenv("COLORTERM"):
            return f"\033]8;;{url}\007{text}\033]8;;\007"
        return text

    def _mergeability_label(self, mergeable: bool | None) -> Literal["mergeable", "conflicting", "unknown"]:
        if mergeable is None:
            return "unknown"
        return "mergeable" if mergeable else "conflicting"

    def get_pr_status_text(
        self,
        pr_state: PRState,
        mergeability: Literal["mergeable", "conflicting", "unknown"],
        is_draft: bool = False,
        merged_at: str | None = None,
    ) -> str:
        # Show draft status first if it's a draft
        if is_draft:
            return "draft"

        # Distinguish between merged and closed based on merged_at
        if pr_state == PRState.CLOSED:
            return "merged" if merged_at else "closed"
        if pr_state == PRState.OPEN:
            if mergeability == "unknown":
                return PRStatus.OPEN_UNKNOWN.display_text
            if mergeability == "mergeable":
                return PRStatus.OPEN_MERGEABLE.display_text
            return PRStatus.OPEN_CONFLICTING.display_text
        return str(pr_state.value).lower()

    def format_status_row(self, name: str, status: StatusResult, pr_info: PRInfo | None, name_width: int = 22) -> str:
        """Format a status row with nice alignment."""
        # Commit hash - vertically aligned column
        commit_short = status.commit_info.short_hash if status.commit_info else "ERROR"

        # Ahead/behind status with light colors, aligned around center point
        sync_status = format_sync_status(status.ahead_count, status.behind_count)

        # Working directory status
        work_status = self._work_status_text(status)

        # GitHub PR status with clickable hyperlinks and clear text
        pr_status = ""
        if isinstance(pr_info, PRInfoOk):
            d: PRData = pr_info.pr_data
            pr_number = d.pr_number
            pr_state = d.pr_state

            # Create clickable hyperlink - fall back to plain text if not supported
            # Hardcoded helper: map PR number -> http://go/pull/{n}
            go_url = urlunparse(("http", "go", f"/pull/{pr_number}", "", "", ""))
            clickable_link = self.make_hyperlink(go_url, f"#{pr_number}")

            # Add lines changed info if available
            lines_info = ""
            if d.additions is not None and d.deletions is not None:
                lines_info = f" +{d.additions}/-{d.deletions}"

            pr_status_text = self.get_pr_status_text(
                pr_state, self._mergeability_label(d.mergeable), d.draft, d.merged_at
            )

            pr_status = f"{clickable_link} {pr_status_text}{lines_info}"

        # Format with nice alignment - commitish as separate column
        # Note: sync_status contains ANSI codes, so we pad the content to exactly 9 chars
        return f"{name:<{name_width}} {commit_short:<10} {sync_status} {work_status:<10} {pr_status}"

    def render_worktree_list(self, worktrees: list[tuple[str, Path, bool]]) -> None:
        if worktrees:
            click.echo("Available worktrees:")
            for name, path, exists in worktrees:
                status = "exists" if exists else "missing"
                click.echo(f"{name}: {path} ({status})")
        else:
            click.echo("No worktrees found")

    def _work_status_text(self, status: StatusResult) -> str:
        """Human-readable working directory status (shared)."""
        dirty_present = status.has_dirty_files
        untracked_present = status.has_untracked_files
        if not status.is_cached:
            return "unknown"
        if dirty_present or untracked_present:
            parts = []
            if dirty_present:
                parts.append("modified")
            if untracked_present:
                parts.append("untracked")
            s = "+".join(parts)
            return f"{s} (stale)" if status.is_stale else s
        return "clean (stale)" if status.is_stale else "clean"

    def _get_pr_link_column(self, status: StatusResult) -> str:
        """Get PR link column."""
        if not isinstance(status.pr_info, PRInfoOk):
            return ""
        pr_number = status.pr_info.pr_data.pr_number
        # Hardcoded helper: map PR number -> http://go/pull/{n}
        go_url = urlunparse(("http", "go", f"/pull/{pr_number}", "", "", ""))
        return self.make_hyperlink(go_url, f"#{pr_number}")

    def _get_pr_status_column(self, status: StatusResult) -> str:
        """Get PR status text column."""
        if isinstance(status.pr_info, PRInfoError):
            return f"github error: {status.pr_info.error}"
        if not isinstance(status.pr_info, PRInfoOk):
            return ""
        d = status.pr_info.pr_data
        return self.get_pr_status_text(d.pr_state, self._mergeability_label(d.mergeable), d.draft, d.merged_at)

    def _get_pr_changes_column(self, status: StatusResult) -> str:
        """Get PR changes (+lines/-lines) column."""
        if not isinstance(status.pr_info, PRInfoOk):
            return ""
        d = status.pr_info.pr_data
        if d.additions is not None and d.deletions is not None:
            return f"+{d.additions}/-{d.deletions}"
        return ""

    def render_top_status_bar(self, status_response) -> None:
        summary = status_response.readiness_summary
        components = status_response.components
        if not summary and not components:
            return
        discovery = "⟳" if (summary and summary.discovery_scanning) else "✓"
        github_state = "ok"
        if components and components.github:
            github_state = components.github.state.value
        elif summary:
            github_state = summary.github.value
        x_of_y = f"{summary.with_gitstatusd}/{summary.total_worktrees}" if summary else "-/-"
        click.echo(f"{discovery} discovery | gitstatusd {x_of_y} | github {github_state}")

    def render_worktree_status_all(self, sorted_items: list[tuple[str, StatusResult]], status_response=None) -> None:
        if not sorted_items:
            click.echo("🤷 No worktrees found")
            return

        # Build table data
        table_data = []
        for name, status in sorted_items:
            pr_info = ""
            pr_link = self._get_pr_link_column(status)
            pr_status = self._get_pr_status_column(status)
            pr_changes = self._get_pr_changes_column(status)
            state_map = {
                "running": "running",
                "restarting": "restarting",
                "failed": "failed",
                "stopped": "stopped",
                "starting": "starting",
            }
            state = state_map.get(status.gitstatusd_state or "", "")
            if pr_link:
                pr_parts = [pr_link]
                if pr_status:
                    pr_parts.append(pr_status)
                if pr_changes:
                    pr_parts.append(pr_changes)
                pr_info = " ".join(pr_parts)

            table_data.append(
                [
                    name,
                    (status.commit_info.short_hash if status.commit_info else "ERROR"),
                    self._work_status_text(status),
                    state,
                    pr_info,
                ]
            )

        # Render table with no headers, no grid lines, just clean aligned columns
        click.echo(tabulate(table_data, tablefmt="plain"))

        # Aggregate and show errors below the table to avoid widening columns
        error_lines = []
        for name, status in sorted_items:
            if status.last_error:
                error_lines.append(f"{name}: {status.last_error}")
        if error_lines:
            click.echo("")
            click.echo("Errors:")
            for ln in error_lines:
                click.echo(f"  - {ln}")
            if self.daemon_log_path:
                click.echo(f"See daemon log: {self.daemon_log_path}")
            else:
                # Fallback: derive from environment if formatter is not configured
                log_path = os.getenv("WT_DIR")
                if log_path:
                    click.echo(f"See daemon log: {Path(log_path) / 'daemon.log'}")

        # Component health summary
        if status_response and status_response.daemon_health:
            dh = status_response.daemon_health
            click.echo("")
            click.echo("Health:")
            click.echo(f"  - status: {dh.status}")
            if dh.last_error:
                click.echo(f"  - last_error: {dh.last_error}")
            click.echo(f"  - counters: github_errors={dh.github_errors}, gitstatusd_errors={dh.gitstatusd_errors}")

    def render_worktree_status_single(self, worktree_name: str, status: StatusResult, pr_info: PRInfo | None) -> None:
        click.echo(f"📊 Status for worktree: {worktree_name}")
        click.echo(f"🔄 {self.format_status_row(worktree_name, status, pr_info)}")

        # Show recent commit details
        if status.commit_info:
            click.echo(f"💬 Last commit: {status.commit_info.message}")
            click.echo(f"👤 Author: {status.commit_info.author} ({status.commit_info.date})")
        else:
            click.echo("💬 Last commit: (unknown)")
            click.echo("👤 Author: (unknown)")

        # Show file status flags (detailed file lists not available in protocol)
        if status.has_dirty_files:
            click.echo("📝 Has modified files")
        if status.has_untracked_files:
            click.echo("❓ Has untracked files")

        # Show PR details if available
        if isinstance(pr_info, PRInfoOk):
            d: PRData = pr_info.pr_data
            pr_number = d.pr_number
            pr_state = d.pr_state

            # Create clickable link for detailed view
            click.echo(
                f"🔗 PR #{pr_number} ({self.make_hyperlink(f'http://go/pull/{pr_number}', f'go/pull/{pr_number}')})"
            )

            # Format detailed PR status
            status_text = self.get_pr_status_text(pr_state, self._mergeability_label(d.mergeable), d.draft, d.merged_at)
            if status_text in PR_STATUS_DISPLAY_MAP:
                icon, message = PR_STATUS_DISPLAY_MAP[status_text]
                click.echo(f"{icon} Status: This PR {message}")
            else:
                click.echo(f"Status: {status_text}")

    def render_worktree_removal_confirmation(self, name: str, worktree_path: Path) -> None:
        click.echo(f"⚠️  About to permanently remove worktree '{name}' at {worktree_path}")

    def render_worktree_removal_success(self, name: str) -> None:
        click.echo(f"✅ Successfully removed worktree '{name}'")
