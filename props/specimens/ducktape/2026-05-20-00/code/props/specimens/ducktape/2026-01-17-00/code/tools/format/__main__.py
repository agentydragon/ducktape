"""Unified formatter that routes files to appropriate formatters.

Based on: https://github.com/aspect-build/rules_lint/blob/main/format/private/format.sh

Unlike rules_lint's format.sh, this handles filenames with special characters correctly
by not using `find` (which breaks on filenames like "-recipe-").

Exclusions: Files with these .gitattributes are skipped (like rules_lint):
    - linguist-generated=true
    - gitlab-generated=true
    - rules-lint-ignored=true

Usage:
    bazel run //tools/format -- file1.py file2.js  # Format specific files
    bazel run //tools/format                        # Format all tracked files

TODO: Support gofmt special case (check stdout, not exit code)
TODO: Support Java/Scala JAVA_RUNFILES workaround
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

# Resolve runfiles paths
from python.runfiles import runfiles

_RUNFILES_OPT = runfiles.Create()
if _RUNFILES_OPT is None:
    raise RuntimeError("Could not create runfiles")
_RUNFILES: runfiles.Runfiles = _RUNFILES_OPT

# Extension -> formatter mapping
EXTENSION_MAP: dict[str, str] = {
    # Prettier
    ".js": "prettier",
    ".jsx": "prettier",
    ".ts": "prettier",
    ".tsx": "prettier",
    ".css": "prettier",
    ".html": "prettier",
    ".md": "prettier",
    ".json": "prettier",
    ".yaml": "prettier",
    ".yml": "prettier",
    ".svelte": "prettier",
    # Ruff
    ".py": "ruff",
    # Shell
    ".sh": "shfmt",
    ".bash": "shfmt",
    # Starlark
    ".bzl": "buildifier",
    ".bazel": "buildifier",
}

# Exact filename -> formatter
FILENAME_MAP: dict[str, str] = {
    "BUILD": "buildifier",
    "BUILD.bazel": "buildifier",
    "WORKSPACE": "buildifier",
    "WORKSPACE.bazel": "buildifier",
}

# Shebang pattern for shell scripts (matching rules_lint)
SHELL_SHEBANG_RE = re.compile(rb"^#![ \t]*/(usr/)?bin/(env[ \t]+)?(sh|bash|mksh|bats|zsh)")


def get_max_batch_size() -> int:
    """Get max command-line size, matching rules_lint behavior."""
    try:
        arg_max = os.sysconf("SC_ARG_MAX")
    except (ValueError, OSError):
        arg_max = 128000
    return min(arg_max - 2048, 128000)


def batch_files(files: list[str], max_size: int) -> list[list[str]]:
    """Split files into batches that fit within ARG_MAX."""
    batches: list[list[str]] = []
    current_batch: list[str] = []
    current_size = 0
    for f in files:
        if current_size + len(f) + 1 >= max_size and current_batch:
            batches.append(current_batch)
            current_batch = []
            current_size = 0
        current_batch.append(f)
        current_size += len(f) + 1
    if current_batch:
        batches.append(current_batch)
    return batches


def detect_shell_by_shebang(path: Path) -> bool:
    """Check if file has a shell shebang (for files without .sh extension)."""
    if path.suffix:  # Has extension, skip shebang check
        return False
    try:
        with path.open("rb") as f:
            first_line = f.readline(256)
        return bool(SHELL_SHEBANG_RE.match(first_line))
    except OSError:
        return False


def get_formatter(path: Path) -> str | None:
    """Determine which formatter to use for a file."""
    if path.name in FILENAME_MAP:
        return FILENAME_MAP[path.name]
    formatter = EXTENSION_MAP.get(path.suffix.lower())
    if formatter:
        return formatter
    # Check shebang for shell scripts without extension (like gradlew)
    if detect_shell_by_shebang(path):
        return "shfmt"
    return None


def get_all_files() -> list[Path]:
    """Get all tracked/modified files via git ls-files."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--modified", "--other", "--exclude-standard"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [Path(f) for f in result.stdout.strip().split("\n") if f]


# Attributes that mark a file as ignored (matching rules_lint behavior)
IGNORE_ATTRIBUTES = ("linguist-generated", "gitlab-generated", "rules-lint-ignored")


def filter_ignored(files: list[Path]) -> list[Path]:
    """Filter out files marked as ignored via .gitattributes."""
    if not files:
        return []

    # Batch check all attributes for all files in one call
    result = subprocess.run(
        ["git", "check-attr", *IGNORE_ATTRIBUTES, "--", *[str(f) for f in files]],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        # If git check-attr fails, don't filter anything
        return files

    # Parse output: "path: attr: value" format
    # A file is ignored if any attribute is "true" (not "unspecified" or "false")
    ignored_files: set[str] = set()
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        # Format: "path: attribute: value"
        parts = line.split(": ", 2)
        if len(parts) == 3 and parts[2] == "true":
            ignored_files.add(parts[0])

    return [f for f in files if str(f) not in ignored_files]


def run_formatter(formatter: str, files: list[Path], check_mode: bool) -> None:
    """Run a formatter on files. Raises on failure."""
    if not files:
        return

    # Filter to existing files
    existing = [str(f) for f in files if f.exists()]
    if not existing:
        return

    # Get binary path from environment (set by Bazel) and resolve via runfiles
    bin_var = f"{formatter.upper()}_BIN"
    rlocation_path = os.environ.get(bin_var)
    if not rlocation_path:
        raise RuntimeError(f"{bin_var} not set")

    bin_path = _RUNFILES.Rlocation(rlocation_path)
    if not bin_path or not Path(bin_path).exists():
        raise RuntimeError(f"could not resolve {rlocation_path}")

    # Build base command (without files)
    if formatter == "prettier":
        base_cmd = [bin_path, "--check" if check_mode else "--write"]
    elif formatter == "ruff":
        base_cmd = [bin_path, "format", *(["--check"] if check_mode else [])]
    elif formatter == "shfmt":
        base_cmd = [bin_path, "-d" if check_mode else "-w"]
    elif formatter == "buildifier":
        base_cmd = [bin_path]
    else:
        raise RuntimeError(f"Unknown formatter: {formatter}")

    # Batch files to avoid ARG_MAX limit
    batches = batch_files(existing, get_max_batch_size())
    start = time.perf_counter()
    for batch in batches:
        subprocess.run([*base_cmd, *batch], check=True)
    elapsed = time.perf_counter() - start
    print(f"Formatted {len(existing)} files with {formatter} in {elapsed:.1f}s")


def main() -> int:
    check_mode = os.environ.get("FMT_CHECK", "").lower() in ("1", "true", "yes")

    # Change to workspace directory if set
    workspace = os.environ.get("BUILD_WORKSPACE_DIRECTORY")
    if workspace:
        os.chdir(workspace)

    # Get files to format
    files = [Path(f) for f in sys.argv[1:]] if len(sys.argv) > 1 else get_all_files()

    # Filter out files marked as ignored via .gitattributes
    files = filter_ignored(files)

    # Group by formatter
    by_formatter: dict[str, list[Path]] = defaultdict(list)
    for f in files:
        formatter = get_formatter(f)
        if formatter:
            by_formatter[formatter].append(f)

    # Run formatters
    try:
        for formatter, formatter_files in by_formatter.items():
            run_formatter(formatter, formatter_files, check_mode)
    except subprocess.CalledProcessError as e:
        print(f"FAILED: A formatter exited with code {e.returncode}", file=sys.stderr)
        if check_mode:
            print("Try running 'bazel run //tools/format' to fix this.", file=sys.stderr)
        raise

    return 0


if __name__ == "__main__":
    sys.exit(main())
