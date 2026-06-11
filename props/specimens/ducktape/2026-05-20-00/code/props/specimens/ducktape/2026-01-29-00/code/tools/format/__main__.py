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

import asyncio
import os
import re
import sys
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pygit2
from python.runfiles import runfiles

from tools.env_utils import get_workspace_dir

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

# Attributes that mark a file as ignored (matching rules_lint behavior)
IGNORE_ATTRIBUTES = ("linguist-generated", "gitlab-generated", "rules-lint-ignored")


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
    batch: list[str] = []
    for f in files:
        if batch and len(" ".join(batch)) + 1 + len(f) >= max_size:
            batches.append(batch)
            batch = []
        batch.append(f)
    return [*batches, batch] if batch else batches


def detect_shell_by_shebang(path: Path) -> bool:
    """Check if file has a shell shebang (for files without .sh extension)."""
    if path.suffix:
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
    if formatter := EXTENSION_MAP.get(path.suffix.lower()):
        return formatter
    if detect_shell_by_shebang(path):
        return "shfmt"
    return None


def get_all_files(repo: pygit2.Repository) -> list[Path]:
    """Get all tracked/modified/untracked files via pygit2."""
    tracked = {entry.path for entry in repo.index}
    modified = {path for path, flags in repo.status().items() if not (flags & pygit2.GIT_STATUS_IGNORED)}
    return [Path(f) for f in sorted(tracked | modified)]


def filter_ignored(repo: pygit2.Repository, files: list[Path]) -> list[Path]:
    """Filter out files marked as ignored via .gitattributes."""
    if not files:
        return []
    return [f for f in files if not any(repo.get_attr(str(f), attr) in (True, "true") for attr in IGNORE_ATTRIBUTES)]


@dataclass
class FormatterResult:
    """Result of running a formatter."""

    formatter: str
    file_count: int
    elapsed: float
    success: bool
    errors: list[str] = field(default_factory=list)


def resolve_formatter_bin(formatter: str) -> str:
    """Resolve formatter binary path from environment. Raises if not found."""
    bin_var = f"{formatter.upper()}_BIN"
    if not (rlocation_path := os.environ.get(bin_var)):
        raise RuntimeError(f"{bin_var} environment variable not set")
    if not (bin_path := _RUNFILES.Rlocation(rlocation_path)) or not Path(bin_path).exists():
        raise RuntimeError(f"Could not resolve {rlocation_path}")
    return bin_path


# Formatter command builders
FORMATTER_COMMANDS: dict[str, Callable[[str, bool], list[str]]] = {
    "prettier": lambda bin_path, check: [bin_path, "--check" if check else "--write"],
    "ruff": lambda bin_path, check: [bin_path, "format", *(["--check"] if check else [])],
    "shfmt": lambda bin_path, check: [bin_path, "-d" if check else "-w"],
    "buildifier": lambda bin_path, _: [bin_path],
}


async def run_batch(base_cmd: list[str], batch: list[str]) -> tuple[int, str]:
    """Run formatter on a batch of files. Returns (returncode, combined output)."""
    proc = await asyncio.create_subprocess_exec(
        *base_cmd, *batch, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    output = (stdout.decode() + stderr.decode()).strip()
    return proc.returncode or 0, output


async def run_formatter_async(formatter: str, files: list[Path], check_mode: bool) -> FormatterResult:
    """Run a formatter on files asynchronously, parallelizing batches."""
    if not files:
        return FormatterResult(formatter=formatter, file_count=0, elapsed=0.0, success=True)

    file_paths = [str(f) for f in files]
    bin_path = resolve_formatter_bin(formatter)
    base_cmd = FORMATTER_COMMANDS[formatter](bin_path, check_mode)

    batches = batch_files(file_paths, get_max_batch_size())
    start = time.perf_counter()

    # Run all batches in parallel
    results = await asyncio.gather(*[run_batch(base_cmd, batch) for batch in batches])

    errors = [output for returncode, output in results if returncode != 0 and output]
    elapsed = time.perf_counter() - start
    return FormatterResult(
        formatter=formatter, file_count=len(file_paths), elapsed=elapsed, success=not errors, errors=errors
    )


async def main_async() -> int:
    check_mode = os.environ.get("FMT_CHECK", "").lower() in ("1", "true", "yes")
    # Workspace dir needed for: pygit2.Repository("."), relative file paths from pre-commit
    os.chdir(get_workspace_dir())

    repo = pygit2.Repository(".")

    # Get files to format
    files = [Path(f) for f in sys.argv[1:]] if len(sys.argv) > 1 else get_all_files(repo)

    # Filter out files marked as ignored via .gitattributes
    files = filter_ignored(repo, files)

    # Group by formatter
    by_formatter: dict[str, list[Path]] = defaultdict(list)
    for f in files:
        if formatter := get_formatter(f):
            by_formatter[formatter].append(f)

    # Run formatters in parallel (batches within each formatter also parallelized)
    start_total = time.perf_counter()
    results = await asyncio.gather(
        *[run_formatter_async(fmt, fmt_files, check_mode) for fmt, fmt_files in by_formatter.items()]
    )

    # Report results
    failed = []
    for result in results:
        if result.file_count > 0:
            status = "✓" if result.success else "✗"
            print(f"{status} {result.formatter}: {result.file_count} files in {result.elapsed:.1f}s")
        if result.errors:
            failed.append(result)
            for error in result.errors:
                print(f"  Error: {error[:500]}", file=sys.stderr)

    elapsed_total = time.perf_counter() - start_total
    print(f"Total: {elapsed_total:.1f}s (parallel)")

    if failed:
        if check_mode:
            print("Try running 'bazel run //tools/format' to fix this.", file=sys.stderr)
        return 1

    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
