"""What a browser is told without asking: one watch over the objects every view reads, fanned out
to the open tabs as SSE.

`inventory.py` and `egress.py` answer a request by listing against the API server and hold nothing
in between; this keeps the same objects between requests instead, so a change reaches a tab when it
happens rather than at the next poll, and one watch serves every tab. The projections are theirs --
`sandbox_views`, `matching_bindings` -- so a pushed row and a fetched one cannot drift.

A frame is a whole snapshot of what the subscription covers, not a delta. Kubernetes watch state is
not a durable log to resume against: a relist replaces a kind wholesale and a `resourceVersion`
expires, so there is no id a reconnecting tab could name. The collections are a handful of objects,
and a snapshot leaves the tab with one code path for connect, change and reconnect alike.

A wedged watch is the failure this must not hide, so every frame carries how long ago each kind
last completed a cycle and the verdict on it: a tab whose data has stopped moving says so instead
of showing a frozen picture as live. The watch never gives up -- `ListWatch` retries with
backoff -- so the verdict, not a dropped connection, is what a stuck API server looks like here.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import CoreV1Api
from pydantic import BaseModel, ConfigDict, Field

from util.kubernetes import CustomObjectsClient
from x.agentplane.app.changes import Changes
from x.agentplane.app.egress import (
    BINDINGS_PLURAL,
    CREDENTIALS_PLURAL,
    EGRESS_API,
    POLICIES_PLURAL,
    BindingView,
    matching_bindings,
)
from x.agentplane.app.inventory import (
    MANAGED_LABEL,
    SANDBOX_API,
    SANDBOXES_PLURAL,
    SandboxView,
    sandbox_view,
    sandbox_views,
)
from x.agentplane.app.trajectory import ThreadView, TrajectoryStore
from x.agentplane.kubernetes_watch import ListWatch, WatchedKind, apply_to

PODS_PLURAL = "pods"

# How many resync periods a kind may miss before the stream calls itself stale, as the egress
# proxy's /healthz counts them: one late cycle is a slow API server, three in a row is a wedge.
STALE_AFTER_CYCLES = 3

# Seconds of silence after which a health frame goes out anyway, so a tab learns its stream has
# gone stale without waiting for a change that is never coming, and proxies keep the stream open.
HEALTH_INTERVAL_S = 15


class WatchHealth(BaseModel):
    """Whether what the frame carries is still moving, and the ages the verdict is drawn from."""

    model_config = ConfigDict(extra="forbid")

    fresh: bool = Field(description="False once any kind has missed its cycles: the data may be stale.")
    stale_after_seconds: float
    refreshed_seconds_ago: dict[str, float] = Field(
        description="Per watched kind, how long ago it last completed a list-and-watch cycle."
    )


class SandboxesSnapshot(BaseModel):
    """A frame of the sandbox-list stream."""

    model_config = ConfigDict(extra="forbid")

    sandboxes: list[SandboxView]
    watch: WatchHealth


class SandboxSnapshot(BaseModel):
    """A frame of one sandbox's stream: the sandbox itself, what may leave it, and its threads."""

    model_config = ConfigDict(extra="forbid")

    sandbox: SandboxView | None = Field(description="None once the sandbox is gone: deleted, or never there.")
    bindings: list[BindingView]
    threads: list[ThreadView]
    watch: WatchHealth


@dataclass
class LiveIndex:
    """The objects the app's views read, kept equal to the API server's by the watch.

    Objects are held as they arrived, and projected per frame: a Sandbox and its Pod make a row
    only together, so joining them early would mean redoing it on every Pod event anyway.
    """

    stale_after_seconds: float
    sandboxes: dict[str, object] = field(default_factory=dict)
    pods: dict[str, k8s_client.V1Pod] = field(default_factory=dict, repr=False)
    bindings: dict[str, object] = field(default_factory=dict)
    policies: dict[str, object] = field(default_factory=dict)
    credentials: dict[str, object] = field(default_factory=dict)
    refreshed: dict[str, datetime] = field(default_factory=dict)
    changes: Changes = field(default_factory=Changes)

    def sandbox_views(self, *, include_archived: bool) -> list[SandboxView]:
        return sandbox_views(self.sandboxes.values(), self.pods.values(), include_archived=include_archived)

    def sandbox_view(self, name: str) -> SandboxView | None:
        raw = self.sandboxes.get(name)
        return None if raw is None else sandbox_view(raw, self.pods.get(name))

    def bindings_for(self, name: str) -> list[BindingView]:
        return matching_bindings(
            self.bindings.values(), self.policies.values(), self.credentials.values(), sandbox=name
        )

    def health(self, now: datetime) -> WatchHealth:
        ages = {kind: (now - at).total_seconds() for kind, at in self.refreshed.items()}
        return WatchHealth(
            fresh=bool(ages) and all(age <= self.stale_after_seconds for age in ages.values()),
            stale_after_seconds=self.stale_after_seconds,
            refreshed_seconds_ago={kind: round(age, 1) for kind, age in sorted(ages.items())},
        )


def _named(raw: dict[str, object]) -> tuple[str, dict[str, object]]:
    metadata = raw["metadata"]
    assert isinstance(metadata, dict)
    return str(metadata["name"]), raw


def _named_pod(pod: k8s_client.V1Pod) -> tuple[str, k8s_client.V1Pod]:
    return str(pod.metadata.name), pod


def watch_for(
    index: LiveIndex,
    *,
    custom_objects: CustomObjectsClient,
    core_v1: CoreV1Api,
    namespace: str,
    sandbox_namespace: str,
    resync_seconds: int,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> ListWatch:
    """The four kinds the app's views are built from, folded into `index` as they change.

    Sandboxes and their Pods come from the namespace they run in; the policy objects from the app's
    own, which is the same split `main.py` gives the inventory and the egress reader.
    """

    async def changed(_kind: WatchedKind) -> None:
        index.changes.notify()

    async def completed(kind: WatchedKind, at: datetime) -> None:
        index.refreshed[kind.name] = at
        index.changes.notify()

    return ListWatch(
        kinds=(
            WatchedKind(
                name=SANDBOXES_PLURAL,
                list=custom_objects.list_namespaced_custom_object,
                args=(*SANDBOX_API, sandbox_namespace, SANDBOXES_PLURAL),
                kwargs={"label_selector": f"{MANAGED_LABEL}=true"},
                parse=_named,
                names=lambda: set(index.sandboxes),
                apply=lambda name, obj: apply_to(index.sandboxes, name, obj),
            ),
            WatchedKind(
                name=PODS_PLURAL,
                list=core_v1.list_namespaced_pod,
                args=(sandbox_namespace,),
                parse=_named_pod,
                names=lambda: set(index.pods),
                apply=lambda name, obj: apply_to(index.pods, name, obj),
            ),
            WatchedKind(
                name=BINDINGS_PLURAL,
                list=custom_objects.list_namespaced_custom_object,
                args=(*EGRESS_API, namespace, BINDINGS_PLURAL),
                parse=_named,
                names=lambda: set(index.bindings),
                apply=lambda name, obj: apply_to(index.bindings, name, obj),
            ),
            WatchedKind(
                name=POLICIES_PLURAL,
                list=custom_objects.list_namespaced_custom_object,
                args=(*EGRESS_API, namespace, POLICIES_PLURAL),
                parse=_named,
                names=lambda: set(index.policies),
                apply=lambda name, obj: apply_to(index.policies, name, obj),
            ),
            WatchedKind(
                name=CREDENTIALS_PLURAL,
                list=custom_objects.list_namespaced_custom_object,
                args=(*EGRESS_API, namespace, CREDENTIALS_PLURAL),
                parse=_named,
                names=lambda: set(index.credentials),
                apply=lambda name, obj: apply_to(index.credentials, name, obj),
            ),
        ),
        resync_seconds=resync_seconds,
        on_change=changed,
        on_cycle=completed,
        clock=clock,
    )


async def frames(
    snapshot: Callable[[], Awaitable[BaseModel]],
    health: Callable[[], WatchHealth],
    *watched: Changes,
    interval_s: float = HEALTH_INTERVAL_S,
) -> AsyncIterator[bytes]:
    """A snapshot per change, and a health frame every `interval_s` of quiet in between.

    Only a change produces a snapshot; the health frames carry the freshness of a stream nothing is
    happening on, which is the reading that separates a quiet watch from a wedged one.
    """
    waiter = asyncio.Event()
    with ExitStack() as stack:
        for changes in watched:
            stack.enter_context(changes.subscribe(waiter))
        while True:
            waiter.clear()
            yield _frame("snapshot", (await snapshot()).model_dump(mode="json"))
            while True:
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=interval_s)
                    break
                except TimeoutError:
                    yield _frame("health", health().model_dump(mode="json"))


def _frame(event: str, data: dict[str, object]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _index(request: Request) -> LiveIndex:
    index = request.app.state.live
    if not isinstance(index, LiveIndex):
        raise TypeError(f"app.state.live is {type(index).__name__}, not LiveIndex")
    return index


def _store(request: Request) -> TrajectoryStore:
    store = request.app.state.store
    if not isinstance(store, TrajectoryStore):
        raise TypeError(f"app.state.store is {type(store).__name__}, not TrajectoryStore")
    return store


Index = Annotated[LiveIndex, Depends(_index)]
Store = Annotated[TrajectoryStore, Depends(_store)]

router = APIRouter(prefix="/live", tags=["live"])

# SSE, so the body is a stream of frames rather than one document; the model is declared for the
# frontend's generated types, which is where the frame shape is actually consumed.
_SANDBOXES_FRAMES: dict[int | str, dict[str, Any]] = {
    200: {"model": SandboxesSnapshot, "content": {"text/event-stream": {}}}
}
_SANDBOX_FRAMES: dict[int | str, dict[str, Any]] = {
    200: {"model": SandboxSnapshot, "content": {"text/event-stream": {}}}
}


def _health(index: LiveIndex) -> WatchHealth:
    return index.health(datetime.now(UTC))


def _stream(source: AsyncIterator[bytes]) -> StreamingResponse:
    return StreamingResponse(
        source, media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


@router.get("/sandboxes", responses=_SANDBOXES_FRAMES)
async def live_sandboxes(
    index: Index, include_archived: Annotated[bool, Query(description="Also carry archived sandboxes.")] = False
) -> StreamingResponse:
    """The sandbox list, pushed."""

    async def snapshot() -> SandboxesSnapshot:
        return SandboxesSnapshot(sandboxes=index.sandbox_views(include_archived=include_archived), watch=_health(index))

    return _stream(frames(snapshot, lambda: _health(index), index.changes))


@router.get("/sandboxes/{name}", responses=_SANDBOX_FRAMES)
async def live_sandbox(index: Index, store: Store, name: str) -> StreamingResponse:
    """One sandbox page, pushed: the sandbox, its bindings, and its threads.

    Threads are not Kubernetes and no watch reaches them; the store notifies when it creates or
    renames one, which is every change a page shows.
    """

    async def snapshot() -> SandboxSnapshot:
        return SandboxSnapshot(
            sandbox=index.sandbox_view(name),
            bindings=index.bindings_for(name),
            threads=await store.list_threads(sandbox=name),
            watch=_health(index),
        )

    return _stream(frames(snapshot, lambda: _health(index), index.changes, store.changes))
