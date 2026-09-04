"""List-and-watch over the four kinds the decision reads, into one `Index`, plus binding status.

The loop itself is `x.agentplane.kubernetes_watch`; this names the kinds and folds each into the `Index`.
Three namespaces, and the split is the point: policies and bindings are read from the operator's
namespace, Sandboxes from the one their Pods run in, Secrets from the credentials namespace.

Bindings' `status` is derived from the index whenever policies or bindings change and when the
nearest expiry passes, and written through the status subresource only when it differs from what
the API server holds.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from kubernetes_asyncio import client as k8s_client
from kubernetes_asyncio.client import CoreV1Api

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
from x.agentplane.kubernetes_watch import ListWatch, WatchedKind, apply_to

logger = logging.getLogger(__name__)

_MERGE_PATCH = "application/merge-patch+json"
# The kinds whose changes can change a binding's status.
_STATUS_KINDS = frozenset({POLICIES_PLURAL, BINDINGS_PLURAL})


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


class Informer:
    def __init__(
        self,
        *,
        index: Index,
        custom_objects: CustomObjectsClient,
        core_v1: CoreV1Api,
        namespace: str,
        sandbox_namespace: str,
        credentials_namespace: str,
        resync_seconds: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._index = index
        self._custom_objects = custom_objects
        self._namespace = namespace
        self._clock = clock
        self._status_dirty = False
        self._watch = ListWatch(
            kinds=(
                WatchedKind(
                    name=POLICIES_PLURAL,
                    list=custom_objects.list_namespaced_custom_object,
                    args=(GROUP, VERSION, namespace, POLICIES_PLURAL),
                    parse=_parse_policy,
                    names=lambda: set(index.policies),
                    apply=lambda name, obj: apply_to(index.policies, name, obj),
                ),
                WatchedKind(
                    name=BINDINGS_PLURAL,
                    list=custom_objects.list_namespaced_custom_object,
                    args=(GROUP, VERSION, namespace, BINDINGS_PLURAL),
                    parse=_parse_binding,
                    names=lambda: set(index.bindings),
                    apply=lambda name, obj: apply_to(index.bindings, name, obj),
                ),
                WatchedKind(
                    name=SANDBOXES_PLURAL,
                    list=custom_objects.list_namespaced_custom_object,
                    args=(SANDBOX_GROUP, SANDBOX_VERSION, sandbox_namespace, SANDBOXES_PLURAL),
                    parse=_parse_sandbox,
                    names=lambda: set(index.sandboxes),
                    apply=lambda name, obj: apply_to(index.sandboxes, name, obj),
                ),
                WatchedKind(
                    name="secrets",
                    list=core_v1.list_namespaced_secret,
                    args=(credentials_namespace,),
                    parse=_parse_secret,
                    names=lambda: set(index.secrets),
                    apply=lambda name, obj: apply_to(index.secrets, name, obj),
                ),
            ),
            resync_seconds=resync_seconds,
            on_change=self._changed,
            on_cycle=self._completed,
            clock=clock,
        )

    async def run(self) -> None:
        """Watch until cancelled."""
        async with asyncio.TaskGroup() as group:
            group.create_task(self._watch.run(), name="egress-informer")
            group.create_task(self._reconcile_statuses_forever(), name="egress-informer-status")

    async def _changed(self, kind: WatchedKind) -> None:
        if kind.name in _STATUS_KINDS:
            self._status_dirty = True
        self._index.synced = self._watch.synced
        await self._index.notify()

    async def _completed(self, kind: WatchedKind, at: datetime) -> None:
        self._index.refreshed[kind.name] = at
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
