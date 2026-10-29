import dataclasses
import errno
import os
import shutil
import subprocess
import sys
from enum import StrEnum
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


def _inspect(
    base: Path, *, proc_root: Path = Path("/proc"), layout: gc.BaseLayout = gc.BaseLayout.HASHED
) -> gc.Inspection:
    return gc.inspect_output_base(base, uid=os.getuid(), points=set(), layout=layout, proc_root=proc_root)


def _record_server(base: Path, *, proc_root: Path, pid: int, argv: list[str]) -> None:
    """Bazel's server.pid.txt pointing at a fake `/proc/<pid>` whose process runs `argv`."""
    (base / "server" / "server.pid.txt").write_text(str(pid))
    (proc_root / str(pid)).mkdir(parents=True)
    (proc_root / str(pid) / "cmdline").write_bytes(b"".join(os.fsencode(argument) + b"\0" for argument in argv))


def _session_env_base(tmp_path: Path, workspace: Path) -> Path:
    """A base at the per-session location Claude Code uses, named like every other one."""
    return make_base(tmp_path / "session-env" / "test-session", workspace, name="bazel-output-base")


def _reason(inspection: gc.Inspection) -> StrEnum:
    """Why a base is kept or sent to review; a prunable base has no reason to be anything else."""
    assert isinstance(inspection, gc.RetainedBase | gc.ReviewBase)
    return inspection.reason


def _described(inspection: gc.Inspection) -> str:
    """The report text of a kept or review base; raises if the inspection lacks the detail its reason cites."""
    assert isinstance(inspection, gc.RetainedBase | gc.ReviewBase)
    return gc.describe_reason(inspection)


def test_missing_workspace_is_immediately_prunable(prunable_base: Path) -> None:
    assert isinstance(_inspect(prunable_base), gc.PrunableBase)


def test_existing_tmp_workspace_is_prunable_immediately(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gc, "_TMP_ROOT", tmp_path / "tmp-root")
    workspace = gc._TMP_ROOT / "workspace"
    workspace.mkdir(parents=True)
    base = make_base(tmp_path / "output", workspace)
    inspection = _inspect(base)
    assert isinstance(inspection, gc.PrunableBase)
    assert inspection.reason is gc.PrunableBaseReason.TMP_WORKSPACE


def test_dangling_workspace_symlink_is_not_prunable(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.symlink_to(tmp_path / "missing-target", target_is_directory=True)
    base = make_base(tmp_path / "output", workspace)
    inspection = _inspect(base)
    assert _reason(inspection) is gc.ReviewBaseReason.WORKSPACE_UNRESOLVABLE
    assert os.strerror(errno.ENOENT) in _described(inspection)


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
    assert _reason(_inspect(prunable_base)) is gc.ReviewBaseReason.UNSAFE_METADATA


def test_fifo_metadata_requires_review_without_blocking(prunable_base: Path) -> None:
    (prunable_base / "README").unlink()
    os.mkfifo(prunable_base / "README")

    assert _reason(_inspect(prunable_base)) is gc.ReviewBaseReason.UNSAFE_METADATA


def test_disagreeing_records_require_review(prunable_base: Path) -> None:
    (prunable_base / "README").write_text("WORKSPACE: /different\n")
    inspection = _inspect(prunable_base)
    assert _reason(inspection) is gc.ReviewBaseReason.UNSAFE_METADATA
    assert "/different" in _described(inspection)


def test_nondefault_output_base_requires_review(tmp_path: Path) -> None:
    workspace = tmp_path / "gone"
    base = make_base(tmp_path / "output", workspace, name="0" * 32)
    inspection = _inspect(base)
    assert _reason(inspection) is gc.ReviewBaseReason.UNSAFE_METADATA
    assert base.name in _described(inspection)


def test_server_process_on_this_base_retains_it(tmp_path: Path) -> None:
    base = make_base(tmp_path / "output", tmp_path / "gone")
    proc_root = tmp_path / "proc"
    _record_server(base, proc_root=proc_root, pid=42, argv=["java", f"--output_base={base}", "--max_idle_secs=10"])

    assert _reason(_inspect(base, proc_root=proc_root)) is gc.RetainedBaseReason.SERVER_LIVE


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["java", "--output_base={other}"], id="another-base's-server"),
        pytest.param(["bash", "-c", "sleep 1"], id="unrelated-process"),
        pytest.param([], id="zombie"),
    ],
)
def test_recycled_server_pid_is_not_live(tmp_path: Path, argv: list[str]) -> None:
    # A sandboxed server records its namespace-local PID, which names some unrelated host
    # process; a host PID is recycled once the server exits. Neither is this base's server.
    base = make_base(tmp_path / "output", tmp_path / "gone")
    other = make_base(tmp_path / "output", tmp_path / "other-gone")
    proc_root = tmp_path / "proc"
    _record_server(base, proc_root=proc_root, pid=42, argv=[argument.format(other=other) for argument in argv])

    assert isinstance(_inspect(base, proc_root=proc_root), gc.PrunableBase)


def test_missing_server_pid_is_not_live(tmp_path: Path) -> None:
    base = make_base(tmp_path / "output", tmp_path / "gone")
    (base / "server" / "server.pid.txt").write_text("42")
    proc_root = tmp_path / "proc"
    proc_root.mkdir()

    assert isinstance(_inspect(base, proc_root=proc_root), gc.PrunableBase)


def test_unreadable_server_cmdline_requires_review(tmp_path: Path) -> None:
    # Injected as a directory (EISDIR) rather than chmod 0, which a root runner reads anyway.
    base = make_base(tmp_path / "output", tmp_path / "gone")
    (base / "server" / "server.pid.txt").write_text("42")
    proc_root = tmp_path / "proc"
    (proc_root / "42" / "cmdline").mkdir(parents=True)

    inspection = _inspect(base, proc_root=proc_root)
    assert _reason(inspection) is gc.ReviewBaseReason.UNSAFE_METADATA
    assert os.strerror(errno.EISDIR) in _described(inspection)


def test_session_env_base_classifies_on_its_records_alone(tmp_path: Path) -> None:
    present = tmp_path / "workspace"
    present.mkdir()
    kept = _inspect(_session_env_base(tmp_path / "a", present), layout=gc.BaseLayout.SESSION_ENV)
    assert _reason(kept) is gc.RetainedBaseReason.WORKSPACE_EXISTS

    prunable = _inspect(_session_env_base(tmp_path / "b", tmp_path / "gone"), layout=gc.BaseLayout.SESSION_ENV)
    assert isinstance(prunable, gc.PrunableBase)
    assert prunable.layout is gc.BaseLayout.SESSION_ENV


def test_session_env_base_still_needs_agreeing_records(tmp_path: Path) -> None:
    # The MD5 anchor is waived for this layout, not the agreement check.
    base = _session_env_base(tmp_path, tmp_path / "gone")
    (base / "README").write_text("WORKSPACE: /different\n")

    assert _reason(_inspect(base, layout=gc.BaseLayout.SESSION_ENV)) is gc.ReviewBaseReason.UNSAFE_METADATA


def test_scan_covers_session_env_bases_and_names_them_by_path(
    prunable_base: Path, output_root: Path, tmp_path: Path, proc: Path, mountinfo: Path
) -> None:
    session_base = _session_env_base(tmp_path, tmp_path / "gone-too")

    inspections = gc.scan_output_user_root(
        output_root, session_bases=[session_base], proc_root=proc, mountinfo_path=mountinfo
    )

    assert [item.path for item in inspections] == [prunable_base, session_base]
    assert all(isinstance(item, gc.PrunableBase) for item in inspections)
    report = gc.render_report(inspections, include_kept=False, include_sizes=False)
    assert session_base.parent.name in report  # the session id, not just the shared directory name


def test_delete_removes_a_prunable_session_env_base(tmp_path: Path, proc: Path, mountinfo: Path) -> None:
    base = _session_env_base(tmp_path, tmp_path / "gone")
    candidate = _inspect(base, layout=gc.BaseLayout.SESSION_ENV)
    assert isinstance(candidate, gc.PrunableBase)

    results = gc.delete_prunable_bases([candidate], proc_root=proc, mountinfo_path=mountinfo)

    assert results == [gc.DeletedBase(base)]
    assert list(base.parent.iterdir()) == []


@pytest.mark.parametrize("value", ["0", "-1"])
def test_impossible_server_pid_requires_review(tmp_path: Path, value: str) -> None:
    base = make_base(tmp_path / "output", tmp_path / "gone")
    (base / "server" / "server.pid.txt").write_text(value)

    assert _reason(_inspect(base)) is gc.ReviewBaseReason.UNSAFE_METADATA


def test_unstatable_output_base_requires_review(tmp_path: Path) -> None:
    inspection = _inspect(tmp_path / "missing")
    assert _reason(inspection) is gc.ReviewBaseReason.CANNOT_STAT
    assert os.strerror(errno.ENOENT) in _described(inspection)


def test_output_base_owned_by_another_user_requires_review(prunable_base: Path) -> None:
    expected_uid = os.getuid() + 54321
    inspection = gc.inspect_output_base(prunable_base, uid=expected_uid, points=set(), layout=gc.BaseLayout.HASHED)

    assert _reason(inspection) is gc.ReviewBaseReason.WRONG_OWNER
    assert str(expected_uid) in _described(inspection)


def test_workspace_that_cannot_be_inspected_requires_review(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("not a directory")
    base = make_base(tmp_path / "output", tmp_path / "file" / "workspace")

    inspection = _inspect(base)
    assert _reason(inspection) is gc.ReviewBaseReason.CANNOT_INSPECT_WORKSPACE
    assert os.strerror(errno.ENOTDIR) in _described(inspection)


def test_workspace_reached_through_a_symlink_requires_review(tmp_path: Path) -> None:
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "workspace").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "real", target_is_directory=True)
    base = make_base(tmp_path / "output", tmp_path / "link" / "workspace")

    inspection = _inspect(base)
    assert _reason(inspection) is gc.ReviewBaseReason.WORKSPACE_RESOLVES_ELSEWHERE
    assert str((tmp_path / "real" / "workspace").resolve()) in _described(inspection)


def test_nested_mount_requires_review(tmp_path: Path) -> None:
    base = make_base(tmp_path / "output", tmp_path / "gone")
    inspection = gc.inspect_output_base(base, uid=os.getuid(), points={base / "nested"}, layout=gc.BaseLayout.HASHED)

    assert _reason(inspection) is gc.ReviewBaseReason.NESTED_MOUNT
    assert str(base / "nested") in _described(inspection)


def test_scan_reports_symlink_and_failed_quarantine(tmp_path: Path, proc: Path, mountinfo: Path) -> None:
    root = tmp_path / "output"
    root.mkdir()
    (root / ("a" * 32)).symlink_to(tmp_path)
    quarantine = root / ".bazel-output-base-gc-dead"
    quarantine.mkdir()

    inspections = gc.scan_output_user_root(root, session_bases=(), proc_root=proc, mountinfo_path=mountinfo)

    assert len(inspections) == 2
    assert {_reason(item) for item in inspections} == {
        gc.ReviewBaseReason.NOT_A_DIRECTORY,
        gc.ReviewBaseReason.INCOMPLETE_QUARANTINE,
    }


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
    inspections = gc.scan_output_user_root(output_root, session_bases=(), proc_root=proc, mountinfo_path=mountinfo)

    assert prunable_base.exists()
    assert [type(item) for item in inspections] == [gc.PrunableBase]
    assert "PRUNE" in gc.render_report(inspections, include_kept=False, include_sizes=False)


def _populated(reason: StrEnum) -> gc.RetainedBase | gc.ReviewBase:
    """The inspection `reason` belongs to, carrying the detail a review reason can cite."""
    base = Path("/test-output/base")
    match reason:
        case gc.RetainedBaseReason():
            return gc.RetainedBase(base, Path("/test-workspace"), reason, 0, False)
        case gc.ReviewBaseReason():
            return gc.ReviewBase(base, reason, 0, detail="test-detail")
    raise TypeError(reason)


@pytest.mark.parametrize("reason", [*gc.RetainedBaseReason, *gc.ReviewBaseReason], ids=str)
def test_every_reason_is_described(reason: StrEnum) -> None:
    assert gc.describe_reason(_populated(reason))


_STATIC_REVIEW_REASONS = {gc.ReviewBaseReason.NOT_A_DIRECTORY, gc.ReviewBaseReason.INCOMPLETE_QUARANTINE}


@pytest.mark.parametrize(
    "reason", [reason for reason in gc.ReviewBaseReason if reason not in _STATIC_REVIEW_REASONS], ids=str
)
def test_a_review_reason_with_detail_renders_it(reason: gc.ReviewBaseReason) -> None:
    assert "test-detail" in gc.describe_reason(_populated(reason))


def test_a_base_whose_workspace_would_be_orphaned_says_so_after_why_it_is_kept(tmp_path: Path) -> None:
    plain = gc.RetainedBase(tmp_path / "base", tmp_path / "workspace", gc.RetainedBaseReason.WORKSPACE_EXISTS, 0, False)
    orphaning = dataclasses.replace(plain, workspace_is_prunable_worktree=True)

    assert _described(orphaning).startswith(_described(plain))
    assert _described(orphaning) != _described(plain)
    assert _described(orphaning) in gc.render_report([orphaning], include_kept=True, include_sizes=False)


if __name__ == "__main__":
    pytest_bazel.main()
