"""Export Syrupy's finished Amber collections as Bazel undeclared test outputs."""

import os
import shutil
from pathlib import Path

import pytest
from syrupy.session import SnapshotSession


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session: pytest.Session) -> None:
    # Syrupy builds its report and deletes obsolete snapshots in its tryfirst sessionfinish hook.
    syrupy: SnapshotSession | None = getattr(session.config, "_syrupy", None)
    if syrupy is None or (report := syrupy.report) is None:
        return
    if not report.update_snapshots:
        if session.exitstatus and (report.num_failed or report.num_unused):
            reporter = session.config.pluginmanager.get_plugin("terminalreporter")
            if reporter is not None:
                target = os.environ.get("TEST_TARGET", "this target")
                reporter.write_line(f"To update snapshots for {target}, see devinfra/docs/syrupy_snapshots.md")
        return

    outputs_dir = os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR")
    if outputs_dir:
        changed = {Path(collection.location) for group in (report.created, report.updated) for collection in group}
        if report.should_delete_unused_snapshots:
            changed.update(Path(collection.location) for collection in report.unused)
        for path in changed:
            if path.is_file() and path.suffix == ".ambr":
                shutil.copy2(path, outputs_dir)
