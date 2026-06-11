"""Prettier formatting helper for generated files."""

import subprocess
from pathlib import Path

from util.bazel.runfiles import get_required_path

_PRETTIER_RLOCATION = "_main/devinfra/prettier_bin_/prettier_bin"


def prettier_format_in_place(path: Path) -> None:
    """Run prettier --write on path to match pre-commit formatting."""
    subprocess.run([get_required_path(_PRETTIER_RLOCATION), "--write", path], check=True)
