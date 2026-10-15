"""VM session workspaces are created by, and Git recognizes, the configured agent identity."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_bazel

from agentplane.runner.harness_process import HarnessProcess
from agentplane.runner.workspace import ensure_workspace

_AGENT_UID = 107
_AGENT_GID = 107


async def _collect(stream: AsyncIterator[bytes]) -> bytes:
    result = bytearray()
    async for chunk in stream:
        result.extend(chunk)
    return bytes(result)


async def _run_as_agent(command: list[str], cwd: Path) -> tuple[int, bytes, bytes]:
    process = HarnessProcess(
        command,
        cwd=cwd,
        environment={
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "HOME": str(cwd),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        },
        state_owner_descriptor=None,
        process_isolation=None,
        agent_uid=_AGENT_UID,
        agent_gid=_AGENT_GID,
    )
    await process.start()
    stdout = asyncio.create_task(_collect(process.output_chunks()))
    stderr = asyncio.create_task(_collect(process.stderr_byte_chunks()))
    try:
        await process.close_stdin()
        exit_code = await process.wait()
        return exit_code, await stdout, await stderr
    finally:
        if process.running:
            await process.stop()
        for task in (stdout, stderr):
            if not task.done():
                task.cancel()
        await asyncio.gather(stdout, stderr, return_exceptions=True)


async def test_fresh_workspace_git_init_and_rev_parse_use_agent_identity() -> None:
    assert os.geteuid() != _AGENT_UID, "this regression requires distinct runner and harness UIDs"
    git = shutil.which("git")
    assert git is not None, "Git must be available in the Linux RBE image"

    # Bazel's TEST_TMPDIR ancestors are private to its executor UID. Put this tiny Git repo
    # directly under /tmp so the configured harness identity can resolve its full working path.
    with tempfile.TemporaryDirectory(prefix="agentplane-workspace-", dir="/tmp") as temporary:
        base = Path(temporary)
        await asyncio.to_thread(base.chmod, 0o755)
        root = base / "workspaces"
        root.mkdir()
        root.chmod(0o777)
        cwd = await ensure_workspace(
            root / "new" / "repository", workspace_root=root, agent_uid=_AGENT_UID, agent_gid=_AGENT_GID
        )

        assert cwd.stat().st_uid == _AGENT_UID
        assert cwd.stat().st_gid == _AGENT_GID
        initialized, _, init_stderr = await _run_as_agent([git, "init", "--quiet"], cwd)
        assert initialized == 0, init_stderr.decode(errors="replace")
        assert (cwd / ".git").stat().st_uid == _AGENT_UID
        assert (cwd / ".git").stat().st_gid == _AGENT_GID

        code, stdout, stderr = await _run_as_agent([git, "rev-parse", "--show-toplevel"], cwd)
        assert code == 0, stderr.decode(errors="replace")
        assert stdout.decode().strip() == str(cwd)


async def test_existing_workspace_data_and_parent_ownership_are_preserved(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    root.chmod(0o777)

    existing = root / "authored"
    existing.mkdir()
    authored_file = existing / "notes.txt"
    authored_file.write_text("keep this ownership")
    existing_owner = (existing.stat().st_uid, existing.stat().st_gid)
    file_owner = (authored_file.stat().st_uid, authored_file.stat().st_gid)
    assert await ensure_workspace(existing, workspace_root=root, agent_uid=_AGENT_UID, agent_gid=_AGENT_GID) == existing
    assert (existing.stat().st_uid, existing.stat().st_gid) == existing_owner
    assert (authored_file.stat().st_uid, authored_file.stat().st_gid) == file_owner

    parent = root / "existing-parent"
    parent.mkdir()
    parent.chmod(0o777)
    parent_owner = (parent.stat().st_uid, parent.stat().st_gid)
    created = await ensure_workspace(
        parent / "new-session" / "nested", workspace_root=root, agent_uid=_AGENT_UID, agent_gid=_AGENT_GID
    )
    assert created.is_dir()
    assert (parent.stat().st_uid, parent.stat().st_gid) == parent_owner
    assert created.stat().st_uid == _AGENT_UID
    assert created.stat().st_gid == _AGENT_GID


async def test_workspace_creation_rejects_symlink_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    root.chmod(0o777)
    outside = tmp_path / "outside"
    (root / "escape").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="must be a child"):
        await ensure_workspace(
            root / "escape" / "new-session", workspace_root=root, agent_uid=_AGENT_UID, agent_gid=_AGENT_GID
        )
    assert not outside.exists()


async def test_rootless_workspace_creation_keeps_the_runner_identity(tmp_path: Path) -> None:
    cwd = tmp_path / "pod" / "new-session"
    created = await ensure_workspace(cwd, workspace_root=None, agent_uid=None, agent_gid=None)
    assert created == cwd
    assert created.is_dir()
    assert created.stat().st_uid == os.geteuid()
    assert created.stat().st_gid == os.getegid()


if __name__ == "__main__":
    pytest_bazel.main()
