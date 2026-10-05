import asyncio

import pytest_bazel

from x.wt.server import wt_server
from x.wt.server.types import DiscoveredWorktree
from x.wt.server.worktree_ids import make_worktree_id
from x.wt.server.wt_server import WtDaemon


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
    assert daemon.worktree_index.get_by_name("created") == created


if __name__ == "__main__":
    pytest_bazel.main()
