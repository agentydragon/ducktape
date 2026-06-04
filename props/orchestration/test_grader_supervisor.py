"""Tests for grader supervisor reconciliation logic.

The supervisor reconciles desired state (DB snapshots) against actual state read
from the runtime (grader pods listed by label). These tests drive a FakeRegistry
that simulates that pod list plus spawn/adopt/reap, and assert the supervisor
adopts healthy graders, reaps duplicates/orphans/wrong-image/terminal pods, and
spawns only what's missing.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
import pytest_bazel

from props.core.ids import SnapshotSlug
from props.orchestration.agent_registry import GraderPodInfo, ImageResolutionError, ResolvedImage
from props.orchestration.executor import PodPhase
from props.orchestration.grader_supervisor import GraderSupervisor

FAKE_IMAGE = ResolvedImage(digest="sha256:abc123", oci_ref="localhost:8000/grader@sha256:abc123")
OLD_IMAGE = ResolvedImage(digest="sha256:old", oci_ref="localhost:8000/grader@sha256:old")

SNAP_A = SnapshotSlug("snap-a")
SNAP_B = SnapshotSlug("snap-b")
SNAP_C = SnapshotSlug("snap-c")


@dataclass
class FakeHandle:
    """Stand-in for AgentRunHandle: deleting it removes the pod from the registry."""

    name: str
    registry: FakeRegistry = field(repr=False)

    async def kill_and_delete(self) -> None:
        self.registry.killed.append(self.name)
        self.registry.pods.pop(self.name, None)


@dataclass
class FakeRegistry:
    """Simulates the runtime grader-pod list plus spawn/adopt/reap."""

    image: ResolvedImage | None = FAKE_IMAGE
    pods: dict[str, GraderPodInfo] = field(default_factory=dict)
    resolve_count: int = 0
    spawned: list[SnapshotSlug] = field(default_factory=list)
    adopted: list[str] = field(default_factory=list)
    reaped: list[str] = field(default_factory=list)
    killed: list[str] = field(default_factory=list)
    _counter: int = 0

    async def resolve_image(self, agent_type: Any, tag: str) -> ResolvedImage:
        self.resolve_count += 1
        if self.image is None:
            raise ImageResolutionError("no image")
        return self.image

    async def list_grader_pods(self) -> list[GraderPodInfo]:
        return list(self.pods.values())

    def add_pod(self, slug: SnapshotSlug, *, image_ref: str, phase: PodPhase = "running") -> GraderPodInfo:
        """Seed a pre-existing pod (e.g. one a previous backend instance started)."""
        self._counter += 1
        name = f"grader-{slug}-{self._counter}"
        pod = GraderPodInfo(name=name, agent_run_id=uuid4(), snapshot_slug=slug, image_ref=image_ref, phase=phase)
        self.pods[name] = pod
        return pod

    async def start_snapshot_grader(
        self, *, image: ResolvedImage, snapshot_slug: SnapshotSlug, model: str
    ) -> FakeHandle:
        pod = self.add_pod(snapshot_slug, image_ref=image.oci_ref)
        self.spawned.append(snapshot_slug)
        return FakeHandle(name=pod.name, registry=self)

    def adopt_grader_pod(self, pod: GraderPodInfo) -> FakeHandle:
        self.adopted.append(pod.name)
        return FakeHandle(name=pod.name, registry=self)

    async def reap_grader_pod(self, pod: GraderPodInfo, *, reason: str) -> None:
        self.reaped.append(pod.name)
        self.pods.pop(pod.name, None)


def _make_supervisor(
    snapshot_slugs: list[SnapshotSlug], *, image: ResolvedImage | None = FAKE_IMAGE, reconcile_debounce_s: float = 0.05
) -> tuple[GraderSupervisor, FakeRegistry]:
    registry = FakeRegistry(image=image)

    @contextmanager
    def fake_session() -> Any:
        session = MagicMock()
        session.query.return_value.all.return_value = [MagicMock(slug=s) for s in snapshot_slugs]
        yield session

    db = MagicMock()
    db.session = fake_session

    gs = GraderSupervisor(
        registry=registry,  # type: ignore[arg-type]
        db_config=MagicMock(),
        model="gpt-5-mini",
        db=db,
        reconcile_debounce_s=reconcile_debounce_s,
        periodic_reconcile_s=0,  # no backstop loop in unit tests
    )
    return gs, registry


def _set_snapshots(gs: GraderSupervisor, slugs: list[SnapshotSlug]) -> None:
    @contextmanager
    def fake_session() -> Any:
        session = MagicMock()
        session.query.return_value.all.return_value = [MagicMock(slug=s) for s in slugs]
        yield session

    gs._db.session = fake_session  # type: ignore[method-assign]


async def test_reconcile_spawns_for_all_snapshots():
    """With no existing pods, reconcile spawns one grader per snapshot."""
    gs, registry = _make_supervisor([SNAP_A, SNAP_B, SNAP_C])
    await gs.reconcile(trigger="test")
    assert sorted(registry.spawned) == sorted([SNAP_A, SNAP_B, SNAP_C])
    assert set(gs._handles) == {SNAP_A, SNAP_B, SNAP_C}


async def test_reconcile_noop_when_image_unavailable():
    gs, registry = _make_supervisor([SNAP_A], image=None)
    await gs.reconcile(trigger="test")
    assert registry.spawned == []
    assert gs._handles == {}


async def test_reconcile_idempotent_keeps_existing():
    """A second reconcile keeps the grader (tracked handle) — no respawn, no reap."""
    gs, registry = _make_supervisor([SNAP_A, SNAP_B])
    await gs.reconcile(trigger="test")
    assert len(registry.spawned) == 2
    await gs.reconcile(trigger="test")
    assert len(registry.spawned) == 2  # unchanged
    assert registry.reaped == []
    assert registry.adopted == []


async def test_reconcile_adopts_orphan_without_respawning():
    """Restart safety: a healthy grader left by a previous instance (pod present,
    no handle) is adopted, not duplicated."""
    gs, registry = _make_supervisor([SNAP_A])
    registry.add_pod(SNAP_A, image_ref=FAKE_IMAGE.oci_ref)  # previous instance's grader
    await gs.reconcile(trigger="startup")
    assert registry.adopted
    assert not registry.spawned
    assert not registry.reaped
    assert SNAP_A in gs._handles


async def test_reconcile_reaps_duplicate():
    """Two pods for one snapshot → keep one, reap the extra."""
    gs, registry = _make_supervisor([SNAP_A])
    registry.add_pod(SNAP_A, image_ref=FAKE_IMAGE.oci_ref)
    registry.add_pod(SNAP_A, image_ref=FAKE_IMAGE.oci_ref)
    await gs.reconcile(trigger="test")
    assert len(registry.reaped) == 1
    assert len(registry.pods) == 1  # one kept


async def test_reconcile_reaps_removed_snapshot():
    """A grader for a snapshot no longer in the DB is reaped."""
    gs, registry = _make_supervisor([])
    registry.add_pod(SNAP_A, image_ref=FAKE_IMAGE.oci_ref)
    await gs.reconcile(trigger="test")
    assert len(registry.reaped) == 1
    assert registry.pods == {}


async def test_reconcile_replaces_wrong_image():
    """A grader on a stale image is reaped and replaced — no restart_existing flag."""
    gs, registry = _make_supervisor([SNAP_A])
    registry.add_pod(SNAP_A, image_ref=OLD_IMAGE.oci_ref)
    await gs.reconcile(trigger="grader_definition_changed:latest")
    assert len(registry.reaped) == 1
    assert registry.spawned == [SNAP_A]
    # exactly one pod, on the new image
    assert len(registry.pods) == 1
    assert next(iter(registry.pods.values())).image_ref == FAKE_IMAGE.oci_ref


async def test_reconcile_reaps_terminal_pod_and_respawns():
    """A crashed (Failed) grader is finalized/reaped and a fresh one spawned."""
    gs, registry = _make_supervisor([SNAP_A])
    registry.add_pod(SNAP_A, image_ref=FAKE_IMAGE.oci_ref, phase="failed")
    await gs.reconcile(trigger="periodic")
    assert len(registry.reaped) == 1
    assert registry.spawned == [SNAP_A]


async def test_reconcile_spawns_new_snapshot_only():
    """A newly added snapshot gets a grader; the existing one is kept."""
    gs, registry = _make_supervisor([SNAP_A])
    await gs.reconcile(trigger="test")
    _set_snapshots(gs, [SNAP_A, SNAP_B])
    await gs.reconcile(trigger="test")
    assert registry.spawned.count(SNAP_B) == 1
    assert set(gs._handles) == {SNAP_A, SNAP_B}
    assert registry.reaped == []


async def test_shutdown_leaves_graders_running():
    """Shutdown must NOT kill graders — the next instance adopts them."""
    gs, registry = _make_supervisor([SNAP_A, SNAP_B])
    await gs.reconcile(trigger="test")
    pods_before = dict(registry.pods)
    await gs.shutdown()
    assert registry.pods == pods_before  # nothing reaped/killed
    assert registry.killed == []
    assert gs._handles == {}


async def test_reconcile_noop_after_shutdown():
    gs, registry = _make_supervisor([SNAP_A])
    await gs.shutdown()
    await gs.reconcile(trigger="test")
    assert registry.spawned == []


# --- Debounce (trailing-edge coalescing) ---


async def test_schedule_reconcile_debounces_burst():
    """A burst of triggers coalesces into a single trailing-edge reconcile."""
    gs, registry = _make_supervisor([SNAP_A, SNAP_B], reconcile_debounce_s=0.05)
    for _ in range(5):
        gs._schedule_reconcile(trigger="grader_definition_changed:latest")
    assert registry.resolve_count == 0  # debounced, not immediate
    await asyncio.sleep(0.2)
    assert registry.resolve_count == 1  # one reconcile for the whole burst
    assert set(gs._handles) == {SNAP_A, SNAP_B}


async def test_schedule_reconcile_delays_but_does_not_drop():
    """The reconcile is delayed by the window, then runs — delay, not cancel."""
    gs, registry = _make_supervisor([SNAP_A], reconcile_debounce_s=0.1)
    gs._schedule_reconcile(trigger="snapshot_created:snap-a")
    assert registry.resolve_count == 0
    await asyncio.sleep(0.25)
    assert registry.resolve_count == 1
    assert SNAP_A in gs._handles


async def test_schedule_reconcile_noop_after_shutdown():
    gs, registry = _make_supervisor([SNAP_A], reconcile_debounce_s=0.05)
    await gs.shutdown()
    gs._schedule_reconcile(trigger="late")
    await asyncio.sleep(0.15)
    assert registry.resolve_count == 0


async def test_reconcile_logs_trigger(caplog: pytest.LogCaptureFixture) -> None:
    """Every reconcile logs its trigger so churn stays attributable."""
    gs, _ = _make_supervisor([SNAP_A])
    with caplog.at_level(logging.INFO, logger="props.orchestration.grader_supervisor"):
        await gs.reconcile(trigger="startup")
    assert "Reconcile triggered by startup" in "\n".join(r.getMessage() for r in caplog.records)


if __name__ == "__main__":
    pytest_bazel.main()
