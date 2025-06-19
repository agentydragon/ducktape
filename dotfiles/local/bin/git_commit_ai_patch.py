#!/usr/bin/env python3
"""
git_commit_ai_patch: launch an interactive Claude Code session using a custom slash command
to generate, review, and apply a git commit based on staged changes.
"""

import argparse
import os
import shutil
import subprocess
import sys


def check_git_repo() -> None:
    try:
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        print("Error: not inside a git repository", file=sys.stderr)
        sys.exit(1)


def get_staged_diff() -> str:
    res = subprocess.run(
        ["git", "diff", "--cached"],
        check=False,
        capture_output=True,
        text=True,
    )
    diff = res.stdout
    if not diff.strip():
        print("No staged changes to commit.", file=sys.stderr)
        sys.exit(1)
    return diff


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Start a Claude Code conversation with /git_commit for interactive commit generation"
        ),
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("GIT_AI_MODEL", "sonnet"),
        help="AI model to use (default: env GIT_AI_MODEL or 'sonnet')",
    )
    args = parser.parse_args()
    check_git_repo()
    diff = get_staged_diff()
    prompt = "/git_commit\n\nStaged diff:\n" + diff
    cli = shutil.which("claude")
    if not cli:
        print("Error: 'claude' CLI not found in PATH", file=sys.stderr)
        sys.exit(1)
    subprocess.run(
        [
            cli,
            "--model",
            args.model,
            prompt,
        ],
        check=False,
    )


if __name__ == "__main__":
    main()
