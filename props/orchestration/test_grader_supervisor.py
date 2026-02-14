"""Tests for grader supervisor reconciliation logic.

Tests the core invariant: reconcile() converges toward one grader per
snapshot when an image is available.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock

import pytest_bazel

from props.core.ids import SnapshotSlug
from props.orchestration.agent_registry import ImageResolutionError, ResolvedImage
from props.orchestration.grader_supervisor import GraderSupervisor

FAKE_IMAGE = ResolvedImage(digest="sha256:abc123", oci_ref="localhost:8000/grader@sha256:abc123")

SNAP_A = SnapshotSlug("snap-a")
SNAP_B = SnapshotSlug("snap-b")
SNAP_C = SnapshotSlug("snap-c")


@dataclass
class FakeHandle:
    """Minimal ContainerHandle stand-in with kill tracking."""

    name: str
    _killed_list: list[FakeHandle] = field(repr=False)

    async def wait(self, *, timeout_seconds: int | None) -> None:
        raise NotImplementedError

    async def kill_and_delete(self) -> None:
        self._killed_list.append(self)


@dataclass
class FakeRegistry:
    """Test double for AgentRegistry — tracks spawned/killed handles."""

    image: ResolvedImage | None = FAKE_IMAGE
    _counter: int = 0
    killed: list[FakeHandle] = field(default_factory=list)

    async def resolve_image(self, agent_type: Any, tag: str) -> ResolvedImage:
        if self.image is None:
            raise ImageResolutionError("no image")
        return self.image

    async def start_snapshot_grader(
        self, *, image: ResolvedImage, snapshot_slug: SnapshotSlug, model: str
    ) -> FakeHandle:
        self._counter += 1
        return FakeHandle(name=f"grader-{self._counter}", _killed_list=self.killed)


def _make_supervisor(
    snapshot_slugs: list[SnapshotSlug], *, image: ResolvedImage | None = FAKE_IMAGE
) -> tuple[GraderSupervisor, FakeRegistry]:
    """Build a GraderSupervisor with a FakeRegistry and mock DB."""
    registry = FakeRegistry(image=image)

    @contextmanager
    def fake_session() -> Any:
        session = MagicMock()
        session.query.return_value.all.return_value = [MagicMock(slug=s) for s in snapshot_slugs]
        yield session

    db = MagicMock()
    db.session = fake_session

    gs = GraderSupervisor(registry=registry, db_config=MagicMock(), model="gpt-5-mini", db=db)  # type: ignore[arg-type]
    return gs, registry


def _mock_session(rows: list[Any]) -> Any:
    """Create a mock session context manager returning given rows."""

    @contextmanager
    def fake_session() -> Any:
        session = MagicMock()
        session.query.return_value.all.return_value = rows
        yield session

    return fake_session


async def test_reconcile_spawns_for_all_snapshots():
    """reconcile() spawns a grader for each snapshot."""
    gs, _ = _make_supervisor([SNAP_A, SNAP_B, SNAP_C])
    await gs.reconcile()
    assert set(gs._handles.keys()) == {SNAP_A, SNAP_B, SNAP_C}


async def test_reconcile_noop_when_image_unavailable():
    """reconcile() does nothing when no grader image is available."""
    gs, _ = _make_supervisor([SNAP_A], image=None)
    await gs.reconcile()
    assert gs._handles == {}


async def test_reconcile_idempotent():
    """Calling reconcile() twice doesn't create duplicate graders."""
    gs, _ = _make_supervisor([SNAP_A, SNAP_B])
    await gs.reconcile()
    handles_first = dict(gs._handles)
    await gs.reconcile()
    # Same handles, not replaced
    assert gs._handles == handles_first


async def test_reconcile_spawns_new_snapshot():
    """reconcile() spawns for a newly added snapshot without touching existing."""
    gs, _ = _make_supervisor([SNAP_A])
    await gs.reconcile()
    handle_a = gs._handles[SNAP_A]

    # Simulate new snapshot appearing in DB
    gs._db.session = _mock_session([MagicMock(slug=s) for s in [SNAP_A, SNAP_B]])  # type: ignore[method-assign]

    await gs.reconcile()
    assert gs._handles[SNAP_A] is handle_a
    assert SNAP_B in gs._handles


async def test_reconcile_kills_removed_snapshot():
    """reconcile() kills grader when snapshot disappears from DB."""
    gs, registry = _make_supervisor([SNAP_A, SNAP_B])
    await gs.reconcile()
    handle_b = gs._handles[SNAP_B]

    # Simulate snap-b removed from DB
    gs._db.session = _mock_session([MagicMock(slug=SNAP_A)])  # type: ignore[method-assign]

    await gs.reconcile()
    assert SNAP_B not in gs._handles
    assert handle_b in registry.killed


async def test_reconcile_restart_kills_and_respawns():
    """reconcile(restart_existing=True) kills all and respawns."""
    gs, registry = _make_supervisor([SNAP_A, SNAP_B])
    await gs.reconcile()
    old_a = gs._handles[SNAP_A]
    old_b = gs._handles[SNAP_B]

    await gs.reconcile(restart_existing=True)
    assert old_a in registry.killed
    assert old_b in registry.killed
    # New handles created
    assert gs._handles[SNAP_A] is not old_a
    assert gs._handles[SNAP_B] is not old_b


async def test_reconcile_restart_also_spawns_missing():
    """restart_existing=True also spawns graders for snapshots not yet tracked."""
    gs, registry = _make_supervisor([SNAP_A])
    await gs.reconcile()
    old_a = gs._handles[SNAP_A]

    # Add snap-b to DB, trigger restart
    gs._db.session = _mock_session([MagicMock(slug=s) for s in [SNAP_A, SNAP_B]])  # type: ignore[method-assign]

    await gs.reconcile(restart_existing=True)
    assert old_a in registry.killed
    assert SNAP_A in gs._handles
    assert SNAP_B in gs._handles


async def test_shutdown_kills_all():
    """shutdown() kills all tracked graders."""
    gs, registry = _make_supervisor([SNAP_A, SNAP_B])
    await gs.reconcile()
    handles = list(gs._handles.values())

    await gs.shutdown()
    assert all(h in registry.killed for h in handles)
    assert gs._handles == {}


async def test_reconcile_noop_after_shutdown():
    """reconcile() does nothing after shutdown."""
    gs, _ = _make_supervisor([SNAP_A])
    await gs.shutdown()
    await gs.reconcile()
    assert gs._handles == {}


if __name__ == "__main__":
    pytest_bazel.main()
