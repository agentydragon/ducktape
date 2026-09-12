"""Prettier formatting helper for generated files."""

import subprocess
from pathlib import Path

from util.bazel.runfiles import get_required_path, own_repo_rlocation

_PRETTIER_RLOCATION = own_repo_rlocation("devinfra/prettier_bin_/prettier_bin")


def prettier_format_in_place(path: Path) -> None:
    """Run prettier --write on path to match pre-commit formatting."""
    subprocess.run([get_required_path(_PRETTIER_RLOCATION), "--write", path], check=True)
