"""Prettier helpers for runtime containers with prettier on PATH."""

import subprocess
from pathlib import Path


def prettier_format_yaml_in_place(path: Path) -> None:
    """Run prettier on YAML with the repo's YAML-relevant formatting options."""
    subprocess.run(
        [
            "prettier",
            "--write",
            "--parser",
            "yaml",
            "--print-width",
            "120",
            "--tab-width",
            "2",
            "--no-use-tabs",
            str(path),
        ],
        check=True,
    )
