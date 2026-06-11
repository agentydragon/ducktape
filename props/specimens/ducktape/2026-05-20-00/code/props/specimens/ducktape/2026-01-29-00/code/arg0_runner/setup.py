from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def _write_file(p: Path, text: str) -> None:
    p.write_text(text, encoding="utf-8")
    mode = p.stat().st_mode
    p.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def setup_arg0_shims(commands: list[str] | None = None) -> Path:
    """Create a temp dir with virtual CLI shims and prepend to PATH.

    Currently installs:
    - apply_patch (and applypatch): dispatches to arg0_runner.runner

    Returns the temp directory path. The caller should keep the process alive;
    the temp directory will be removed by the OS at some later time.
    """
    cmds = (set(commands) if commands is not None else set()) | {"apply_patch", "applypatch"}
    tmp = Path(tempfile.mkdtemp(prefix="adgn-arg0-"))
    runner = tmp / "adgn-arg0"
    # Write a small shim that invokes the module by name for portability
    _write_file(
        runner,
        """#!/usr/bin/env python3
import os, sys
from arg0_runner.runner import main
sys.exit(main())
""",
    )
    # UNIX-only symlinks for each command
    for name in cmds:
        link = tmp / name
        link.symlink_to(runner)
    # Prepend to PATH
    os.environ["PATH"] = f"{tmp}{os.pathsep}" + os.environ.get("PATH", "")
    return tmp
