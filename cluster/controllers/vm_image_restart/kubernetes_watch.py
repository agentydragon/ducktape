"""List-and-watch over Kubernetes kinds, folded into stores owned by the caller.

Every kind runs its own loop: a list replaces that kind wholesale (the resync), a watch from the
list's `resourceVersion` applies changes until the server ends it after `resync_seconds`, and the
loop lists again. A failed cycle backs off and relists.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from kubernetes_asyncio import watch as k8s_watch
from tenacity import AsyncRetrying, before_sleep_log, wait_exponential

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchedKind[K, T]:
    """One kind to keep in sync: how to list it, and how to fold objects into the caller's store."""

    name: str
    list: Callable[..., Awaitable[Any]]
    args: tuple[Any, ...]
    key: Callable[[T], K]
    names: Callable[[], set[K]]
    apply: Callable[[K, T | None], None]
    kwargs: Mapping[str, Any] = field(default_factory=dict)


class ListWatch:
    def __init__(
        self, *, kinds: Sequence[WatchedKind], resync_seconds: int, on_change: Callable[[WatchedKind], Awaitable[None]]
    ) -> None:
        self._kinds = tuple(kinds)
        self._resync_seconds = resync_seconds
        self._on_change = on_change

    async def run(self) -> None:
        """Watch until cancelled."""
        async with asyncio.TaskGroup() as group:
            for kind in self._kinds:
                group.create_task(self._watch_forever(kind), name=f"list-watch-{kind.name}")

    async def _watch_forever(self, kind: WatchedKind) -> None:
        while True:
            async for attempt in AsyncRetrying(
                wait=wait_exponential(max=30), before_sleep=before_sleep_log(logger, logging.WARNING)
            ):
                with attempt:
                    await self._cycle(kind)

    async def _cycle(self, kind: WatchedKind) -> None:
        listed = await kind.list(*kind.args, **kind.kwargs)
        # Custom objects list as a dict; core kinds as a typed list.
        items = listed["items"] if isinstance(listed, dict) else listed.items
        version = (
            listed["metadata"]["resourceVersion"] if isinstance(listed, dict) else listed.metadata.resource_version
        )
        parsed = {kind.key(obj): obj for obj in items}
        for key in kind.names() - set(parsed):
            kind.apply(key, None)
        for key, obj in parsed.items():
            kind.apply(key, obj)
        await self._on_change(kind)
        watcher = k8s_watch.Watch()
        try:
            # Integer seconds: the API server parses timeoutSeconds with strconv.ParseInt, so a float
            # reaches it as "300.0" and every watch is refused with 400.
            async for event in watcher.stream(
                kind.list, *kind.args, **kind.kwargs, resource_version=version, timeout_seconds=self._resync_seconds
            ):
                match event["type"]:
                    case "ADDED" | "MODIFIED":
                        obj = event["object"]
                        kind.apply(kind.key(obj), obj)
                    case "DELETED":
                        kind.apply(kind.key(event["object"]), None)
                    case "BOOKMARK":
                        continue
                    case other:
                        raise RuntimeError(f"watch of {kind.name} returned event type {other!r}")
                await self._on_change(kind)
        finally:
            watcher.stop()


def apply_to[K, T](store: dict[K, T], key: K, obj: T | None) -> None:
    """The usual `apply`: replace the object under its key, or remove it when `obj` is `None`."""
    if obj is None:
        store.pop(key, None)
    else:
        store[key] = obj
