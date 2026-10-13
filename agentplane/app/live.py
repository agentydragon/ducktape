"""What a browser is told without asking: one watch over the objects every view reads, fanned out
to the open tabs as SSE.

The Sandbox Service's `kubernetes_views.py` and `egress_views.py` answer a request by listing
against the API server and hold nothing in between; this keeps the same objects between requests
instead, so a change reaches a tab when it happens rather than at the next poll, and one watch
serves every tab. The projections are theirs -- `sandbox_views`, `matching_bindings` -- so a pushed
row and a fetched one cannot drift. The action
policy is the Action Service's answer, not a projection of anything held here: the two policy kinds
are watched only so that an event on either re-asks the service.

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
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
import httpx2
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import CoreV1Api
from pydantic import BaseModel, ConfigDict, Field

from agentplane import sandbox_resource
from agentplane.action_service.policies import resources
from agentplane.app.action_federation import OperatorFederationError, operator_actions, upstream_failure_detail
from agentplane.app.action_policy import ActionPolicyInventory, ActionPolicyUnavailable, ActionPolicyView
from agentplane.app.bound_policy import BoundPolicyView, bound_policy_view
from agentplane.app.database_updates import Channel
from agentplane.app.identity import CallerIdentity, require_caller
from agentplane.app.sandbox_models import SandboxView, sandbox_view as http_sandbox_view
from agentplane.app.shutdown import Shutdown
from agentplane.app.threads.store import MAX_PAGE, ThreadStore
from agentplane.app.threads.thread_view import ThreadView
from agentplane.changes import ChangeSignal
from agentplane.crd_group import GROUP, VERSION
from agentplane.egress.decisions import Decision
from agentplane.egress.decisions_client import DecisionsClient, DecisionsUnavailableError
from agentplane.grpc_calls import ServiceError
from agentplane.kubernetes_watch import Freshness
from agentplane.policy_sets import POLICY_SETS_PLURAL
from agentplane.postgres.wakeups import WakeupListener
from agentplane.sandbox_resource import Sandbox
from agentplane.sandbox_service.action_policy_views import ACTION_POLICY_API
from agentplane.sandbox_service.egress_views import (
    BINDINGS_PLURAL,
    CREDENTIALS_PLURAL,
    EGRESS_API,
    POLICIES_PLURAL,
    BindingView,
    matching_bindings,
)
from agentplane.sandbox_service.kubernetes_views import incarnation_pod, sandbox_view, sandbox_views
from agentplane.sse import frame, sse_response
from agentplane.subjects import ServiceAccountRef
from util.kubernetes import CustomObjectsClient
from util.kubernetes_watch import ListWatch, WatchedKind, apply_to

PODS_PLURAL = "pods"

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


class ThreadsSnapshot(SandboxesSnapshot):
    threads: list[ThreadView] = Field(
        description="The newest threads of the listing, newest first, then the open thread if it is not among them."
    )
    more: bool = Field(description="Whether the listing has threads past these.")
    archived_count: int = Field(description="Archived threads in all, whether or not the listing carries them.")
    updates_connected: bool = Field(description="Whether this replica is listening for PostgreSQL Thread changes.")


class ThreadsUnavailable(BaseModel):
    """The Runner Gateway could not say which Sessions run in the sandbox, so its threads are not
    known; carried in the frame so the rest of the page, its lifecycle controls included, still
    shows."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["unavailable"] = "unavailable"
    code: str = Field(description="The gRPC status code the Runner Gateway answered with.")


class DecisionsUnavailable(BaseModel):
    """Why the egress proxy's decisions could not be read; the frame says it rather than ending the stream."""

    model_config = ConfigDict(extra="forbid")

    detail: str


class DecisionsWatching(BaseModel):
    """Whether this replica is following the proxy's change stream. While it is not, what a frame
    carries is as old as the last change heard of."""

    model_config = ConfigDict(extra="forbid")

    watching: bool


class DecisionsSnapshot(DecisionsWatching):
    """A frame of one sandbox's egress decision stream."""

    decisions: list[Decision] | DecisionsUnavailable = Field(description="Oldest first.")


class SandboxSnapshot(BaseModel):
    """A frame of one sandbox's stream: the sandbox itself, what may leave it, what the Action
    Service auto-decides for it, and its threads."""

    model_config = ConfigDict(extra="forbid")

    sandbox: SandboxView | None = Field(description="None once the sandbox is gone: deleted, or never there.")
    bindings: list[BindingView]
    action_policy: ActionPolicyView | ActionPolicyUnavailable | None = Field(
        description="The Action Service's answer for the sandbox, or why it could not be asked; None only with "
        "the sandbox, since a policy subject pins its UID and a gone sandbox has none."
    )
    bound_policy: BoundPolicyView | None = Field(
        description="Every rule of the PolicySets the sandbox names; None only with the sandbox."
    )
    threads: list[ThreadView] | ThreadsUnavailable
    watch: WatchHealth


@dataclass
class LiveIndex:
    """The objects the app's views read, kept equal to the API server's by the watch.

    Objects are held as they arrived, and projected per frame: a Sandbox and its Pod make a row
    only together, so joining them early would mean redoing it on every Pod event anyway.
    """

    freshness: Freshness
    core_v1: CoreV1Api
    sandboxes: dict[str, object] = field(default_factory=dict)
    pods: dict[str, k8s_client.V1Pod] = field(default_factory=dict, repr=False)
    bindings: dict[str, object] = field(default_factory=dict)
    policies: dict[str, object] = field(default_factory=dict)
    credentials: dict[str, object] = field(default_factory=dict)
    rule_sets: dict[str, object] = field(default_factory=dict)
    # The action policy kinds are a trigger, not a copy: the Action Service's informer holds the
    # objects, and the sandbox stream asks it again whenever this moves. Only the names stay, for a
    # relist to reconcile against.
    action_policy_sets: set[str] = field(default_factory=set)
    action_policy_bindings: set[str] = field(default_factory=set)
    action_policy_changes: int = 0
    changes: ChangeSignal = field(default_factory=ChangeSignal)

    def sandbox_views(self) -> list[SandboxView]:
        # kubernetes_asyncio exposes this runtime attribute but omits it from generated SDK stubs.
        api_client = self.core_v1.api_client  # type: ignore[attr-defined]
        return [
            http_sandbox_view(view)
            for view in sandbox_views(self.sandboxes.values(), self.pods.values(), api_client=api_client)
        ]

    def sandbox_view(self, name: str) -> SandboxView | None:
        raw = self.sandboxes.get(name)
        if raw is None:
            return None
        # kubernetes_asyncio exposes this runtime attribute but omits it from generated SDK stubs.
        api_client = self.core_v1.api_client  # type: ignore[attr-defined]
        parsed = Sandbox.model_validate(raw)
        return http_sandbox_view(sandbox_view(parsed, incarnation_pod(parsed, self.pods), api_client=api_client))

    def bound_policy_for(self, name: str) -> BoundPolicyView | None:
        raw = self.sandboxes.get(name)
        if raw is None:
            return None
        return bound_policy_view(Sandbox.model_validate(raw).spec.policy_sets, self.rule_sets.values())

    def bindings_for(self, subject: ServiceAccountRef) -> list[BindingView]:
        return matching_bindings(
            self.bindings.values(), self.policies.values(), self.credentials.values(), subject=subject
        )

    def action_policy_seen(self, names: set[str], name: str, obj: object | None) -> None:
        """`apply` for the two policy kinds: keep the name, bump the trigger."""
        if obj is None:
            names.discard(name)
        else:
            names.add(name)
        self.action_policy_changes += 1

    def health(self, now: datetime) -> WatchHealth:
        return WatchHealth(
            fresh=self.freshness.fresh(now),
            stale_after_seconds=self.freshness.stale_after_seconds,
            refreshed_seconds_ago={kind: round(age, 1) for kind, age in self.freshness.ages(now).items()},
        )


def _name(raw: dict[str, object]) -> str:
    metadata = raw["metadata"]
    assert isinstance(metadata, dict)
    return str(metadata["name"])


def _pod_name(pod: k8s_client.V1Pod) -> str:
    return str(pod.metadata.name)


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
    """The kinds the app's views are built from, folded into `index` as they change.

    Sandboxes, their Pods and the action policy objects come from the namespace the sandboxes run
    in; the egress objects from the app's own, which is the same split `main.py` gives the
    inventory, the egress reader and the action policy reader.
    """

    async def changed(_kind: WatchedKind) -> None:
        index.changes.notify()

    async def completed(kind: WatchedKind, at: datetime) -> None:
        index.freshness.record(kind.name, at)
        index.changes.notify()

    return ListWatch(
        kinds=(
            WatchedKind(
                name=sandbox_resource.SANDBOXES_PLURAL,
                list=custom_objects.list_namespaced_custom_object,
                args=(GROUP, VERSION, sandbox_namespace, sandbox_resource.SANDBOXES_PLURAL),
                key=_name,
                names=lambda: set(index.sandboxes),
                apply=lambda name, obj: apply_to(index.sandboxes, name, obj),
            ),
            WatchedKind(
                name=PODS_PLURAL,
                list=core_v1.list_namespaced_pod,
                args=(sandbox_namespace,),
                key=_pod_name,
                names=lambda: set(index.pods),
                apply=lambda name, obj: apply_to(index.pods, name, obj),
            ),
            WatchedKind(
                name=BINDINGS_PLURAL,
                list=custom_objects.list_namespaced_custom_object,
                args=(*EGRESS_API, namespace, BINDINGS_PLURAL),
                key=_name,
                names=lambda: set(index.bindings),
                apply=lambda name, obj: apply_to(index.bindings, name, obj),
            ),
            WatchedKind(
                name=POLICIES_PLURAL,
                list=custom_objects.list_namespaced_custom_object,
                args=(*EGRESS_API, namespace, POLICIES_PLURAL),
                key=_name,
                names=lambda: set(index.policies),
                apply=lambda name, obj: apply_to(index.policies, name, obj),
            ),
            WatchedKind(
                name=CREDENTIALS_PLURAL,
                list=custom_objects.list_namespaced_custom_object,
                args=(*EGRESS_API, namespace, CREDENTIALS_PLURAL),
                key=_name,
                names=lambda: set(index.credentials),
                apply=lambda name, obj: apply_to(index.credentials, name, obj),
            ),
            WatchedKind(
                name=f"{POLICY_SETS_PLURAL}.{GROUP}",
                list=custom_objects.list_namespaced_custom_object,
                args=(GROUP, VERSION, sandbox_namespace, POLICY_SETS_PLURAL),
                key=_name,
                names=lambda: set(index.rule_sets),
                apply=lambda name, obj: apply_to(index.rule_sets, name, obj),
            ),
            WatchedKind(
                name=resources.POLICY_SETS_PLURAL,
                list=custom_objects.list_namespaced_custom_object,
                args=(*ACTION_POLICY_API, sandbox_namespace, resources.POLICY_SETS_PLURAL),
                key=_name,
                names=lambda: set(index.action_policy_sets),
                apply=lambda name, obj: index.action_policy_seen(index.action_policy_sets, name, obj),
            ),
            WatchedKind(
                name=resources.BINDINGS_PLURAL,
                list=custom_objects.list_namespaced_custom_object,
                args=(*ACTION_POLICY_API, sandbox_namespace, resources.BINDINGS_PLURAL),
                key=_name,
                names=lambda: set(index.action_policy_bindings),
                apply=lambda name, obj: index.action_policy_seen(index.action_policy_bindings, name, obj),
            ),
        ),
        resync_seconds=resync_seconds,
        on_change=changed,
        on_cycle=completed,
        clock=clock,
    )


async def frames(
    snapshot: Callable[[], Awaitable[BaseModel]],
    health: Callable[[], BaseModel],
    *watched: ChangeSignal,
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
            yield frame("snapshot", (await snapshot()).model_dump(mode="json"))
            while True:
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=interval_s)
                    break
                except TimeoutError:
                    yield frame("health", health().model_dump(mode="json"))


async def action_policy_frame(
    request: Request, caller: CallerIdentity, inventory: ActionPolicyInventory, subject: ServiceAccountRef
) -> ActionPolicyView | ActionPolicyUnavailable:
    """The Action Service's answer for the subject, or why it could not be asked, carried in the
    frame rather than ending a stream that also carries the sandbox itself."""
    try:
        return await inventory.for_subject(operator_actions(request, caller), subject)
    except OperatorFederationError as error:
        return ActionPolicyUnavailable(code=str(error))
    except (httpx.HTTPStatusError, httpx.RequestError, httpx2.HTTPStatusError, httpx2.TransportError) as error:
        return ActionPolicyUnavailable(code="upstream_request_failed", upstream=upstream_failure_detail(error))


async def threads_frame(
    store: ThreadStore, sandbox: str, *, include_archived: bool
) -> list[ThreadView] | ThreadsUnavailable:
    try:
        return await store.list_threads(sandbox=sandbox, include_archived=include_archived)
    except ServiceError as error:
        return ThreadsUnavailable(code=error.code.name)


class ActionPolicyFrames:
    """One stream's action policy: asked of the service again when a policy object changed or the
    stream is following another subject, and otherwise repeated, so a Pod or thread event does not
    cost an exchange with the identity provider. A failure is never kept; the next change retries.

    Keyed by the subject asked about, which is the whole of what the answer depends on: two
    sandboxes running as one account share it, and a replacement sandbox with an account of its own
    misses it.
    """

    def __init__(
        self,
        index: LiveIndex,
        fetch: Callable[[ServiceAccountRef], Awaitable[ActionPolicyView | ActionPolicyUnavailable]],
    ) -> None:
        self._index = index
        self._fetch = fetch
        self._kept: tuple[ServiceAccountRef, int, ActionPolicyView] | None = None

    async def for_subject(self, subject: ServiceAccountRef) -> ActionPolicyView | ActionPolicyUnavailable:
        seen = self._index.action_policy_changes
        if self._kept is not None and self._kept[:2] == (subject, seen):
            return self._kept[2]
        frame = await self._fetch(subject)
        self._kept = (subject, seen, frame) if isinstance(frame, ActionPolicyView) else None
        return frame


def _index(request: Request) -> LiveIndex:
    index = request.app.state.services.live
    if not isinstance(index, LiveIndex):
        raise TypeError(f"app.state.services.live is {type(index).__name__}, not LiveIndex")
    return index


def _action_policy(request: Request) -> ActionPolicyInventory:
    inventory = request.app.state.services.action_policy
    if not isinstance(inventory, ActionPolicyInventory):
        raise TypeError(f"app.state.services.action_policy is {type(inventory).__name__}, not ActionPolicyInventory")
    return inventory


def _decisions(request: Request) -> DecisionsClient:
    decisions = request.app.state.services.decisions
    if not isinstance(decisions, DecisionsClient):
        raise TypeError(f"app.state.services.decisions is {type(decisions).__name__}, not DecisionsClient")
    return decisions


def _store(request: Request) -> ThreadStore:
    store = request.app.state.services.store
    if not isinstance(store, ThreadStore):
        raise TypeError(f"app.state.services.store is {type(store).__name__}, not ThreadStore")
    return store


def _database_updates(request: Request) -> WakeupListener:
    updates = request.app.state.services.database_updates
    if not isinstance(updates, WakeupListener):
        raise TypeError(f"app.state.services.database_updates is {type(updates).__name__}, not WakeupListener")
    return updates


Index = Annotated[LiveIndex, Depends(_index)]
Store = Annotated[ThreadStore, Depends(_store)]
Decisions = Annotated[DecisionsClient, Depends(_decisions)]
Updates = Annotated[WakeupListener, Depends(_database_updates)]
ActionPolicy = Annotated[ActionPolicyInventory, Depends(_action_policy)]
Caller = Annotated[CallerIdentity, Depends(require_caller)]

router = APIRouter(prefix="/live", tags=["live"])

# SSE, so the body is a stream of frames rather than one document; the model is declared for the
# frontend's generated types, which is where the frame shape is actually consumed.
_SANDBOXES_FRAMES: dict[int | str, dict[str, Any]] = {
    200: {"model": SandboxesSnapshot, "content": {"text/event-stream": {}}}
}
_THREADS_FRAMES: dict[int | str, dict[str, Any]] = {
    200: {"model": ThreadsSnapshot, "content": {"text/event-stream": {}}}
}
_SANDBOX_FRAMES: dict[int | str, dict[str, Any]] = {
    200: {"model": SandboxSnapshot, "content": {"text/event-stream": {}}}
}
_DECISIONS_FRAMES: dict[int | str, dict[str, Any]] = {
    200: {"model": DecisionsSnapshot, "content": {"text/event-stream": {}}}
}


def _health(index: LiveIndex) -> WatchHealth:
    return index.health(datetime.now(UTC))


@router.get("/sandboxes", responses=_SANDBOXES_FRAMES)
async def live_sandboxes(index: Index, shutdown: Shutdown) -> StreamingResponse:
    """The sandbox list, pushed."""

    async def snapshot() -> SandboxesSnapshot:
        return SandboxesSnapshot(sandboxes=index.sandbox_views(), watch=_health(index))

    return sse_response(shutdown.until(frames(snapshot, lambda: _health(index), index.changes)))


@router.get("/threads", responses=_THREADS_FRAMES)
async def live_threads(
    index: Index,
    store: Store,
    updates: Updates,
    shutdown: Shutdown,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE, description="Carry at most this many of the listing.")] = 50,
    search: Annotated[
        str | None,
        Query(
            min_length=1, description="Only threads whose name or sandbox contains this, or whose ID starts with it."
        ),
    ] = None,
    include_archived: Annotated[bool, Query(description="Also list archived threads.")] = False,
    open_thread: Annotated[
        UUID | None, Query(description="Also carry this thread, whether or not the listing reaches it.")
    ] = None,
) -> StreamingResponse:
    """The sidebar's window onto the thread list, pushed: a reader who wants more asks again with a
    larger `limit`, so every thread it shows stays current."""

    async def snapshot() -> ThreadsSnapshot:
        page = await store.list_page(limit=limit, search=search, include_archived=include_archived)
        threads = page.threads
        if open_thread is not None and all(thread.id != open_thread for thread in threads):
            opened = await store.get_thread(open_thread)
            threads = threads if opened is None else [*threads, opened]
        return ThreadsSnapshot(
            sandboxes=index.sandbox_views(),
            threads=threads,
            more=page.next_page_token is not None,
            archived_count=await store.archived_count(),
            updates_connected=updates.listener.connected,
            watch=_health(index),
        )

    return sse_response(
        shutdown.until(frames(snapshot, lambda: _health(index), index.changes, updates.changes[Channel.THREADS]))
    )


@router.get("/sandboxes/{name}", responses=_SANDBOX_FRAMES)
async def live_sandbox(
    request: Request,
    caller: Caller,
    index: Index,
    store: Store,
    updates: Updates,
    action_policy: ActionPolicy,
    shutdown: Shutdown,
    name: str,
    include_archived: Annotated[bool, Query(description="Also carry archived threads.")] = False,
) -> StreamingResponse:
    """One sandbox page, pushed: the sandbox, its bindings, its action policy, and its threads.

    Threads are not Kubernetes and no watch reaches them; the store notifies when it creates,
    renames, or archives one, which is every change a page shows. The action policy is the Action
    Service's answer, asked as the operator this session is; the watch on the policy kinds only says
    when to ask again.
    """
    policy = ActionPolicyFrames(index, lambda subject: action_policy_frame(request, caller, action_policy, subject))

    async def snapshot() -> SandboxSnapshot:
        sandbox = index.sandbox_view(name)
        return SandboxSnapshot(
            sandbox=sandbox,
            bindings=[] if sandbox is None else index.bindings_for(sandbox.service_account),
            action_policy=None if sandbox is None else await policy.for_subject(sandbox.service_account),
            bound_policy=index.bound_policy_for(name),
            threads=await threads_frame(store, name, include_archived=include_archived),
            watch=_health(index),
        )

    return sse_response(
        shutdown.until(frames(snapshot, lambda: _health(index), index.changes, updates.changes[Channel.THREADS]))
    )


@router.get("/sandboxes/{name}/egress/decisions", responses=_DECISIONS_FRAMES)
async def live_egress_decisions(index: Index, decisions: Decisions, shutdown: Shutdown, name: str) -> StreamingResponse:
    """What the egress proxy recently admitted or refused for the sandbox's account, pushed.

    The proxy keeps no copy per tab: it says when decisions were committed, and each frame re-reads
    them from it. A proxy that cannot be asked is carried in the frame, not by ending the stream.
    """

    async def snapshot() -> DecisionsSnapshot:
        sandbox = index.sandbox_view(name)
        if sandbox is None:
            return DecisionsSnapshot(
                watching=decisions.watching, decisions=DecisionsUnavailable(detail=f"no sandbox {name}")
            )
        try:
            recent = await decisions.recent(sandbox.service_account)
        except DecisionsUnavailableError as error:
            return DecisionsSnapshot(watching=decisions.watching, decisions=DecisionsUnavailable(detail=str(error)))
        return DecisionsSnapshot(watching=decisions.watching, decisions=recent)

    return sse_response(
        shutdown.until(frames(snapshot, lambda: DecisionsWatching(watching=decisions.watching), decisions.changes))
    )
