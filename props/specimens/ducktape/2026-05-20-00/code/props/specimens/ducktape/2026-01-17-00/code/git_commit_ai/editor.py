from __future__ import annotations

import asyncio
import os
import sys
from importlib import resources
from pathlib import Path

import pygit2
from mako.template import Template

# Marker used by git to separate commit message from commented metadata (without # prefix)
SCISSORS_MARK = "------------------------ >8 ------------------------"


def get_editor(repo: pygit2.Repository) -> str:
    """Get the editor to use for commit messages.

    Follows git's precedence: GIT_EDITOR env > core.editor config >
    VISUAL env > EDITOR env > 'vi'
    """
    try:
        git_editor = repo.config["core.editor"]
    except KeyError:
        git_editor = None

    for candidate in (os.environ.get("GIT_EDITOR"), git_editor, os.environ.get("VISUAL"), os.environ.get("EDITOR")):
        if candidate:
            return candidate
    return "vi"


async def run_editor(repo: pygit2.Repository, content: str) -> str | None:
    """Open editor with content, return edited commit message or None if aborted."""
    commit_msg_path = Path(repo.path) / "COMMIT_EDITMSG"
    commit_msg_path.write_text(content)
    mtime_before = commit_msg_path.stat().st_mtime

    editor = get_editor(repo)
    editor_proc = await asyncio.create_subprocess_shell(f"{editor} {commit_msg_path}")
    if (rc := await editor_proc.wait()) != 0:
        print(f"Aborting commit: editor exited with code {rc} (e.g., :cq)", file=sys.stderr)
        return None

    try:
        final_content = commit_msg_path.read_text()
        mtime_after = commit_msg_path.stat().st_mtime
        changed = final_content.rstrip("\n") != content
        if mtime_after == mtime_before and not changed:
            print("Aborting commit: editor closed without saving (unchanged commit message).", file=sys.stderr)
            return None
    except FileNotFoundError:
        print("Aborting commit.", file=sys.stderr)
        return None

    return extract_commit_content(final_content)


def render_editor_content(
    repo: pygit2.Repository,
    msg: str,
    *,
    cached: bool,
    elapsed_s: float,
    verbose: bool = False,
    user_context: str | None = None,
    previous_message: str | None = None,
) -> str:
    """Build editor content: AI message followed by commented metadata."""
    template_text = resources.files(__package__).joinpath("commit_editmsg.mako").read_text("utf-8")
    rendered: str = Template(template_text).render(
        repo=repo,
        verbose=verbose,
        user_context=user_context,
        previous_message=previous_message,
        cached=cached,
        elapsed_s=elapsed_s,
        scissors_mark=SCISSORS_MARK,
    )
    return msg + "\n\n" + rendered.strip()


def extract_commit_content(text: str) -> str:
    """Extract commit content, stopping at scissors mark and removing comments."""
    content_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith(SCISSORS_MARK):
            break
        if not line.strip().startswith("#"):
            content_lines.append(line)
    return "\n".join(content_lines).strip()
