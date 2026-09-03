"""List-and-watch over the four kinds the decision reads, into one `Index`, plus binding status.

Each kind runs its own loop: a list replaces that kind wholesale (the resync), a watch from the
list's `resourceVersion` applies the changes until the server ends it after `resync_seconds`, and
the loop lists again. A failed cycle backs off and relists. Policies, bindings, and Sandboxes are
read from the sandbox namespace; Secrets from the credentials namespace only.

Bindings' `status` is derived from the index whenever policies or bindings change and when the
nearest expiry passes, and written through the status subresource only when it differs from what
the API server holds.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from kubernetes_asyncio import client as k8s_client, watch as k8s_watch
from kubernetes_asyncio.client import CoreV1Api
from tenacity import AsyncRetrying, before_sleep_log, wait_exponential

from util.kubernetes import CustomObjectsClient
from x.agentplane.egress.policy import Index, binding_status
from x.agentplane.egress.resources import (
    BINDINGS_PLURAL,
    GROUP,
    POLICIES_PLURAL,
    SANDBOX_GROUP,
    SANDBOX_VERSION,
    SANDBOXES_PLURAL,
    VERSION,
    EgressBinding,
    EgressPolicy,
    Sandbox,
    Secret,
)

logger = logging.getLogger(__name__)

_MERGE_PATCH = "application/merge-patch+json"


@dataclass(frozen=True)
class _Kind:
    """One watched kind: how to list it, and how to fold one object (or its deletion) into the index."""

    plural: str
    list: Callable[..., Awaitable[Any]]
    args: tuple[Any, ...]
    parse: Callable[[Any], tuple[str, Any]]
    names: Callable[[Index], set[str]]
    apply: Callable[[Index, str, Any | None], None]
    # Whether a change here can change a binding's status.
    affects_status: bool


def _parse_policy(raw: dict[str, Any]) -> tuple[str, EgressPolicy]:
    policy = EgressPolicy.model_validate(raw)
    return policy.metadata.name, policy


def _parse_binding(raw: dict[str, Any]) -> tuple[str, EgressBinding]:
    binding = EgressBinding.model_validate(raw)
    return binding.metadata.name, binding


def _parse_sandbox(raw: dict[str, Any]) -> tuple[str, Sandbox]:
    sandbox = Sandbox.model_validate(raw)
    return sandbox.metadata.name, sandbox


def _parse_secret(raw: k8s_client.V1Secret) -> tuple[str, Secret]:
    secret = Secret.from_v1(raw)
    return secret.name, secret


def _apply_policy(index: Index, name: str, policy: EgressPolicy | None) -> None:
    if policy is None:
        index.policies.pop(name, None)
    else:
        index.policies[name] = policy


def _apply_binding(index: Index, name: str, binding: EgressBinding | None) -> None:
    if binding is None:
        index.bindings.pop(name, None)
    else:
        index.bindings[name] = binding


def _apply_sandbox(index: Index, name: str, sandbox: Sandbox | None) -> None:
    if sandbox is None:
        index.sandboxes.pop(name, None)
    else:
        index.sandboxes[name] = sandbox


def _apply_secret(index: Index, name: str, secret: Secret | None) -> None:
    if secret is None:
        index.secrets.pop(name, None)
    else:
        index.secrets[name] = secret


class Informer:
    def __init__(
        self,
        *,
        index: Index,
        custom_objects: CustomObjectsClient,
        core_v1: CoreV1Api,
        namespace: str,
        credentials_namespace: str,
        resync_seconds: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._index = index
        self._custom_objects = custom_objects
        self._namespace = namespace
        self._resync_seconds = resync_seconds
        self._clock = clock
        self._status_dirty = False
        self._synced: set[str] = set()
        self._kinds = (
            _Kind(
                plural=POLICIES_PLURAL,
                list=custom_objects.list_namespaced_custom_object,
                args=(GROUP, VERSION, namespace, POLICIES_PLURAL),
                parse=_parse_policy,
                names=lambda index: set(index.policies),
                apply=_apply_policy,
                affects_status=True,
            ),
            _Kind(
                plural=BINDINGS_PLURAL,
                list=custom_objects.list_namespaced_custom_object,
                args=(GROUP, VERSION, namespace, BINDINGS_PLURAL),
                parse=_parse_binding,
                names=lambda index: set(index.bindings),
                apply=_apply_binding,
                affects_status=True,
            ),
            _Kind(
                plural=SANDBOXES_PLURAL,
                list=custom_objects.list_namespaced_custom_object,
                args=(SANDBOX_GROUP, SANDBOX_VERSION, namespace, SANDBOXES_PLURAL),
                parse=_parse_sandbox,
                names=lambda index: set(index.sandboxes),
                apply=_apply_sandbox,
                affects_status=False,
            ),
            _Kind(
                plural="secrets",
                list=core_v1.list_namespaced_secret,
                args=(credentials_namespace,),
                parse=_parse_secret,
                names=lambda index: set(index.secrets),
                apply=_apply_secret,
                affects_status=False,
            ),
        )

    async def run(self) -> None:
        """Watch until cancelled."""
        async with asyncio.TaskGroup() as group:
            for kind in self._kinds:
                group.create_task(self._watch_forever(kind), name=f"egress-informer-{kind.plural}")
            group.create_task(self._reconcile_statuses_forever(), name="egress-informer-status")

    async def _watch_forever(self, kind: _Kind) -> None:
        while True:
            async for attempt in AsyncRetrying(
                wait=wait_exponential(max=30), before_sleep=before_sleep_log(logger, logging.WARNING)
            ):
                with attempt:
                    await self._cycle(kind)

    async def _cycle(self, kind: _Kind) -> None:
        listed = await kind.list(*kind.args)
        # Custom objects list as a dict; core kinds as a typed list.
        items = listed["items"] if isinstance(listed, dict) else listed.items
        version = (
            listed["metadata"]["resourceVersion"] if isinstance(listed, dict) else listed.metadata.resource_version
        )
        parsed = dict(kind.parse(item) for item in items)
        for name in kind.names(self._index) - set(parsed):
            kind.apply(self._index, name, None)
        for name, obj in parsed.items():
            kind.apply(self._index, name, obj)
        self._synced.add(kind.plural)
        self._index.synced = len(self._synced) == len(self._kinds)
        await self._changed(kind)
        watcher = k8s_watch.Watch()
        try:
            async for event in watcher.stream(
                kind.list, *kind.args, resource_version=version, timeout_seconds=self._resync_seconds
            ):
                match event["type"]:
                    case "ADDED" | "MODIFIED":
                        name, obj = kind.parse(event["object"])
                        kind.apply(self._index, name, obj)
                    case "DELETED":
                        name, _ = kind.parse(event["object"])
                        kind.apply(self._index, name, None)
                    case "BOOKMARK":
                        continue
                    case other:
                        raise RuntimeError(f"watch of {kind.plural} returned event type {other!r}")
                await self._changed(kind)
        finally:
            watcher.stop()

    async def _changed(self, kind: _Kind) -> None:
        if kind.affects_status:
            self._status_dirty = True
        await self._index.notify()

    async def _reconcile_statuses_forever(self) -> None:
        await self._index.wait_for(lambda: self._index.synced)
        while True:
            self._status_dirty = False
            await self._reconcile_statuses()
            timeout = self._seconds_to_next_expiry()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._index.wait_for(lambda: self._status_dirty), timeout)

    def _seconds_to_next_expiry(self) -> float | None:
        now = self._clock()
        upcoming = [
            (binding.spec.expires_at - now).total_seconds()
            for binding in self._index.bindings.values()
            if binding.spec.expires_at is not None and binding.spec.expires_at > now
        ]
        return min(upcoming) if upcoming else None

    async def _reconcile_statuses(self) -> None:
        now = self._clock().replace(microsecond=0)
        for name, binding in list(self._index.bindings.items()):
            desired = binding_status(self._index, binding, now)
            if binding.status == desired:
                continue
            await self._custom_objects.patch_namespaced_custom_object_status(
                GROUP,
                VERSION,
                self._namespace,
                BINDINGS_PLURAL,
                name,
                {"status": desired.model_dump(by_alias=True, mode="json")},
                _content_type=_MERGE_PATCH,
            )
            # Keep the index ahead of the watch's echo of this write, so a reconcile in between
            # compares equal instead of writing the same status again.
            if self._index.bindings.get(name) is binding:
                self._index.bindings[name] = binding.model_copy(update={"status": desired})
            logger.info("binding %s status: %s", name, desired.conditions[0].reason)
