import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_bazel

from devinfra.gc import output_base_gc as gc
from devinfra.gc.conftest import make_base


@pytest.fixture
def output_root(tmp_path: Path) -> Path:
    return tmp_path / "output"


@pytest.fixture
def prunable_base(tmp_path: Path, output_root: Path) -> Path:
    """The canonical PRUNE candidate: a base whose recorded workspace is absent."""
    return make_base(output_root, tmp_path / "gone")


def _inspect(base: Path, *, proc_root: Path = Path("/proc")) -> gc.Inspection:
    return gc.inspect_output_base(base, uid=os.getuid(), points=set(), proc_root=proc_root)


def test_missing_workspace_is_immediately_prunable(prunable_base: Path) -> None:
    assert isinstance(_inspect(prunable_base), gc.PrunableBase)


def test_existing_workspace_is_retained(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    base = make_base(tmp_path / "output", workspace)
    assert isinstance(_inspect(base), gc.RetainedBase)


def test_dangling_workspace_symlink_is_not_prunable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    base = make_base(tmp_path / "output", workspace)
    assert isinstance(_inspect(base), gc.ReviewBase)


@pytest.mark.parametrize("metadata", ["README", "DO_NOT_BUILD_HERE", "server/cmdline"])
def test_single_missing_record_still_classifies(prunable_base: Path, metadata: str) -> None:
    # Any one record may be gone — notably server/cmdline for a server that never
    # persisted it — and the surviving records still recover the workspace, so the
    # base is prunable rather than stuck in review.
    (prunable_base / metadata).unlink()
    assert isinstance(_inspect(prunable_base), gc.PrunableBase)


def test_single_surviving_record_is_enough(prunable_base: Path) -> None:
    # Two records gone, one left: the base name == md5(workspace) still anchors it.
    (prunable_base / "server" / "cmdline").unlink()
    (prunable_base / "DO_NOT_BUILD_HERE").unlink()
    assert isinstance(_inspect(prunable_base), gc.PrunableBase)


def test_all_records_absent_requires_review(prunable_base: Path) -> None:
    for metadata in ("README", "DO_NOT_BUILD_HERE", "server/cmdline"):
        (prunable_base / metadata).unlink()
    assert isinstance(_inspect(prunable_base), gc.ReviewBase)


def test_fifo_metadata_requires_review_without_blocking(prunable_base: Path) -> None:
    (prunable_base / "README").unlink()
    os.mkfifo(prunable_base / "README")

    assert isinstance(_inspect(prunable_base), gc.ReviewBase)


def test_disagreeing_records_require_review(prunable_base: Path) -> None:
    (prunable_base / "README").write_text("WORKSPACE: /different\n")
    assert isinstance(_inspect(prunable_base), gc.ReviewBase)


def test_nondefault_output_base_requires_review(tmp_path: Path) -> None:
    workspace = tmp_path / "gone"
    base = make_base(tmp_path / "output", workspace, name="0" * 32)
    assert isinstance(_inspect(base), gc.ReviewBase)


def test_existing_server_pid_retains_base(tmp_path: Path) -> None:
    base = make_base(tmp_path / "output", tmp_path / "gone")
    (base / "server" / "server.pid.txt").write_text("42")
    proc_root = tmp_path / "proc"
    (proc_root / "42").mkdir(parents=True)

    assert isinstance(_inspect(base, proc_root=proc_root), gc.RetainedBase)


def test_missing_server_pid_is_not_live(tmp_path: Path) -> None:
    base = make_base(tmp_path / "output", tmp_path / "gone")
    (base / "server" / "server.pid.txt").write_text("42")
    proc_root = tmp_path / "proc"
    proc_root.mkdir()

    assert isinstance(_inspect(base, proc_root=proc_root), gc.PrunableBase)


@pytest.mark.parametrize("value", ["0", "-1"])
def test_impossible_server_pid_requires_review(tmp_path: Path, value: str) -> None:
    base = make_base(tmp_path / "output", tmp_path / "gone")
    (base / "server" / "server.pid.txt").write_text(value)

    assert isinstance(_inspect(base), gc.ReviewBase)


def test_nested_mount_requires_review(tmp_path: Path) -> None:
    base = make_base(tmp_path / "output", tmp_path / "gone")
    assert isinstance(gc.inspect_output_base(base, uid=os.getuid(), points={base / "nested"}), gc.ReviewBase)


def test_scan_reports_symlink_and_failed_quarantine(tmp_path: Path, proc: Path, mountinfo: Path) -> None:
    root = tmp_path / "output"
    root.mkdir()
    (root / ("a" * 32)).symlink_to(tmp_path)
    quarantine = root / ".bazel-output-base-gc-dead"
    quarantine.mkdir()

    inspections = gc.scan_output_user_root(root, proc_root=proc, mountinfo_path=mountinfo)

    assert len(inspections) == 2
    assert all(isinstance(item, gc.ReviewBase) for item in inspections)


def test_mount_points_unescapes_kernel_path_encoding(tmp_path: Path) -> None:
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(r"36 25 0:32 / /tmp/mounted\040path rw - ext4 /dev/root rw" + "\n")

    assert gc.mount_points(mountinfo_path=mountinfo) == {Path("/tmp/mounted path")}


def test_report_tolerates_size_failure(prunable_base: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    inspection = _inspect(prunable_base)
    assert isinstance(inspection, gc.PrunableBase)
    monkeypatch.setattr(gc, "allocated_bytes", lambda _path: None)

    report = gc.render_report([inspection], include_kept=False, include_sizes=True)

    assert "?" in report
    assert "prunable size unavailable" in report


def test_delete_handles_read_only_directories(prunable_base: Path, proc: Path, mountinfo: Path) -> None:
    nested = prunable_base / "execroot" / "nested"
    nested.mkdir(parents=True)
    (nested / "artifact").write_text("data")
    nested.chmod(0o555)
    nested.parent.chmod(0o555)
    candidate = _inspect(prunable_base)
    assert isinstance(candidate, gc.PrunableBase)

    results = gc.delete_prunable_bases([candidate], proc_root=proc, mountinfo_path=mountinfo)

    assert results == [gc.DeletedBase(prunable_base)]
    assert not prunable_base.exists()


def test_delete_rechecks_workspace_absence(tmp_path: Path, proc: Path, mountinfo: Path) -> None:
    workspace = tmp_path / "workspace"
    base = make_base(tmp_path / "output", workspace)
    candidate = _inspect(base)
    assert isinstance(candidate, gc.PrunableBase)
    workspace.mkdir()

    results = gc.delete_prunable_bases([candidate], proc_root=proc, mountinfo_path=mountinfo)

    assert isinstance(results[0], gc.SkippedBase)
    assert base.exists()


def test_delete_rechecks_mounts_under_lock(tmp_path: Path) -> None:
    base = make_base(tmp_path / "output", tmp_path / "gone")
    candidate = _inspect(base)
    assert isinstance(candidate, gc.PrunableBase)
    proc = tmp_path / "proc"
    proc.mkdir()
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(f"36 25 0:32 / {base}/nested rw - ext4 /dev/root rw\n")

    results = gc.delete_prunable_bases([candidate], proc_root=proc, mountinfo_path=mountinfo)

    assert isinstance(results[0], gc.SkippedBase)
    assert base.exists()


def _rmtree_unreadable_subtree(*_args: object, **_kwargs: object) -> None:
    raise PermissionError("unreadable directory")


def test_rmtree_permission_error_fails_with_the_quarantine_kept(
    tmp_path: Path, proc: Path, mountinfo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unreadable subtree is injected rather than made: chmod(0) does not stop a root runner,
    # which would take the delete-succeeds path instead.
    base = make_base(tmp_path / "output", tmp_path / "gone")
    candidate = _inspect(base)
    assert isinstance(candidate, gc.PrunableBase)
    monkeypatch.setattr(shutil, "rmtree", _rmtree_unreadable_subtree)

    [result] = gc.delete_prunable_bases([candidate], proc_root=proc, mountinfo_path=mountinfo)

    assert isinstance(result, gc.FailedBase)
    assert result.quarantine is not None
    assert result.quarantine.is_dir()
    assert not base.exists()


def test_rmtree_callback_does_not_retry_open_with_the_wrong_signature(tmp_path: Path) -> None:
    error = PermissionError("unreadable directory")

    with pytest.raises(PermissionError) as raised:
        gc._retry_rmtree_after_permission_error(os.open, str(tmp_path), error)

    assert raised.value is error


def test_bazel_lock_conflicts_with_another_process(tmp_path: Path) -> None:
    workspace = tmp_path / "gone"
    base = make_base(tmp_path / "output", workspace)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import fcntl, os, sys; fd = os.open(sys.argv[1], os.O_RDWR); "
            "fcntl.lockf(fd, fcntl.LOCK_EX, 1, 0, os.SEEK_SET); print('locked', flush=True); sys.stdin.read(1)",
            str(base / "lock"),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline() == "locked\n"
        with pytest.raises(gc.MetadataError), gc._bazel_lock(base, uid=os.getuid()):
            pass
    finally:
        assert holder.stdin is not None
        holder.communicate("x", timeout=5)


def test_scan_and_render_report_flag_a_prunable_base(
    prunable_base: Path, output_root: Path, proc: Path, mountinfo: Path
) -> None:
    inspections = gc.scan_output_user_root(output_root, proc_root=proc, mountinfo_path=mountinfo)

    assert prunable_base.exists()
    assert [type(item) for item in inspections] == [gc.PrunableBase]
    assert "PRUNE" in gc.render_report(inspections, include_kept=False, include_sizes=False)


if __name__ == "__main__":
    pytest_bazel.main()
