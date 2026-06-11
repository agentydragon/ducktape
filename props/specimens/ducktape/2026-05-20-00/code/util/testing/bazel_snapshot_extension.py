"""Syrupy extension and pytest plugin for Bazel snapshot workflows.

When TEST_UNDECLARED_OUTPUTS_DIR is set (Bazel test sandbox), copies each
written .ambr file there so it can be downloaded after an RBE test run.

Also prints a hint with the update command when snapshots fail.

Wired automatically by `uses_syrupy = True` in the `py_test` macro.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest
from syrupy.extensions.amber import AmberSnapshotExtension


class BazelAmberExtension(AmberSnapshotExtension):
    """Amber extension that copies written snapshots to undeclared test outputs."""

    @classmethod
    def write_snapshot_collection(cls, *, snapshot_collection):
        super().write_snapshot_collection(snapshot_collection=snapshot_collection)
        outputs_dir = os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR")
        if not outputs_dir:
            return
        src = Path(snapshot_collection.location)
        if not src.exists():
            return
        dest = Path(outputs_dir) / src.name
        shutil.copy2(src, dest)


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter, exitstatus: int, config: pytest.Config) -> None:
    """Print snapshot update command hint when snapshots fail."""
    syrupy_session = getattr(config, "_syrupy", None)
    if not syrupy_session:
        return
    report = getattr(syrupy_session, "report", None)
    if not report:
        return
    # Don't print hint if this was already an update run
    if getattr(config.option, "update_snapshots", False):
        return
    # Only hint when tests actually failed (snapshot mismatches cause assertion errors)
    if exitstatus == 0:
        return
    target = os.environ.get("TEST_TARGET", "//path/to:target")
    terminalreporter.write_line("")
    terminalreporter.write_line("To update snapshots:")
    terminalreporter.write_line(
        f"  bb test --config=rbe --remote_download_outputs=toplevel {target}"
        " --test_arg=--snapshot-update --nocache_test_results"
    )
    terminalreporter.write_line(
        f"  cp bazel-testlogs/{target.lstrip('/').replace(':', '/')}/test.outputs/*.ambr <source>/__snapshots__/"
    )
