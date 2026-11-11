import asyncio
from unittest.mock import AsyncMock, Mock

import psutil
import pytest
import pytest_bazel

from x.wt.server import wt_server
from x.wt.server.types import DiscoveredWorktree
from x.wt.server.worktree_ids import make_worktree_id
from x.wt.server.wt_server import WtDaemon


async def test_discovery_reconciles_added_removed_and_existing_worktrees(real_config, monkeypatch):
    daemon = WtDaemon(real_config)
    removed = DiscoveredWorktree(real_config.worktrees_dir / "removed", "removed", make_worktree_id("removed"))
    retained = DiscoveredWorktree(real_config.worktrees_dir / "retained", "retained", make_worktree_id("retained"))
    refreshed = DiscoveredWorktree(retained.path, "refreshed", retained.wtid)
    added = DiscoveredWorktree(real_config.worktrees_dir / "added", "added", make_worktree_id("added"))
    scan = AsyncMock(side_effect=[{removed, retained}, {refreshed, added}])
    start = AsyncMock()
    stop = AsyncMock()
    monkeypatch.setattr(wt_server, "scan_worktrees", scan)
    monkeypatch.setattr(daemon, "_start_gitstatusd_for_worktree", start)
    monkeypatch.setattr(daemon, "_stop_gitstatusd_for_worktree", stop)

    await daemon._run_discovery_once()
    start.reset_mock()
    await daemon._run_discovery_once()

    start.assert_awaited_once_with(added)
    stop.assert_awaited_once_with(removed)
    assert daemon.store.worktree_paths() == {refreshed.path, added.path}
    assert daemon.worktree_index is not None
    assert daemon.worktree_index.by_name["refreshed"] == refreshed
    assert "removed" not in daemon.worktree_index.by_name


async def test_registration_survives_discovery_scan_that_predates_it(real_config, monkeypatch):
    daemon = WtDaemon(real_config)
    scan_started = asyncio.Event()
    release_scan = asyncio.Event()

    async def scan_before_creation(_worktrees_dir):
        scan_started.set()
        await release_scan.wait()
        return set()

    async def start_nothing(_info):
        return None

    monkeypatch.setattr(wt_server, "scan_worktrees", scan_before_creation)
    monkeypatch.setattr(daemon, "_start_gitstatusd_for_worktree", start_nothing)

    discovery = asyncio.create_task(daemon._run_discovery_once())
    await scan_started.wait()
    created = DiscoveredWorktree(real_config.worktrees_dir / "created", "created", make_worktree_id("created"))
    registration = asyncio.create_task(daemon.register_worktree(created))
    release_scan.set()
    await asyncio.gather(discovery, registration)

    assert daemon.worktree_index is not None
    assert daemon.worktree_index.by_name.get("created") == created


async def test_worktree_found_by_a_scan_while_being_registered_gets_one_gitstatusd(real_config, monkeypatch):
    daemon = WtDaemon(real_config)
    created = DiscoveredWorktree(real_config.worktrees_dir / "created", "created", make_worktree_id("created"))
    created.path.mkdir()
    scan_started = asyncio.Event()
    release_scan = asyncio.Event()

    async def scan_that_sees_the_worktree(_worktrees_dir):
        scan_started.set()
        await release_scan.wait()
        return {created}

    monkeypatch.setattr(wt_server, "scan_worktrees", scan_that_sees_the_worktree)

    def gitstatusd_children() -> list[psutil.Process]:
        return [p for p in psutil.Process().children() if p.cmdline()[:1] == [str(real_config.gitstatusd_path)]]

    try:
        discovery = asyncio.create_task(daemon._run_discovery_once())
        await scan_started.wait()
        registration = asyncio.create_task(daemon.register_worktree(created))
        release_scan.set()
        await asyncio.gather(discovery, registration)

        assert len(gitstatusd_children()) == 1
    finally:
        await daemon.stop()
        for orphan in gitstatusd_children():
            orphan.kill()


async def test_failed_initial_discovery_fails_the_startup_handshake(real_config, monkeypatch):
    handshakes = []
    monkeypatch.setattr(wt_server, "write_startup_handshake", lambda **kwargs: handshakes.append(kwargs))

    async def refuse_scan(_worktrees_dir):
        raise OSError("scan refused")

    monkeypatch.setattr(wt_server, "scan_worktrees", refuse_scan)
    daemon = WtDaemon(real_config)

    try:
        with pytest.raises(OSError, match="scan refused"):
            await daemon.start()
    finally:
        await daemon.stop()

    assert not any(handshake.get("ready") for handshake in handshakes)
    assert handshakes[-1]["success"] is False
    assert "scan refused" in handshakes[-1]["error_message"]


async def test_stop_cancels_periodic_discovery(real_config):
    daemon = WtDaemon(real_config)
    started = asyncio.Event()

    async def pending_discovery():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(pending_discovery())
    daemon.discovery_task = task
    await started.wait()
    await daemon.stop()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_stop_clears_git_watchers_even_if_one_fails(real_config):
    daemon = WtDaemon(real_config)
    failed = Mock(stop=AsyncMock(side_effect=RuntimeError("failed to stop")))
    healthy = Mock(stop=AsyncMock())
    daemon.git_watchers[make_worktree_id("failed")] = failed
    daemon.git_watchers[make_worktree_id("healthy")] = healthy

    await daemon.stop()

    failed.stop.assert_awaited_once()
    healthy.stop.assert_awaited_once()
    assert daemon.git_watchers == {}


if __name__ == "__main__":
    pytest_bazel.main()
