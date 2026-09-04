"""List-and-watch over a set of Kubernetes kinds, each folded into a store the caller owns.

Every kind runs its own loop: a list replaces that kind wholesale (the resync), a watch from the
list's `resourceVersion` applies the changes until the server ends it after `resync_seconds`, and
the loop lists again. A failed cycle backs off and relists.

`on_cycle` is the part worth reading before reusing this: it fires only past a watch the server
ended on schedule, so a wedged list, a watch that never returns, or one the server keeps refusing
stops advancing it while every answer the store gives stays plausible. A reader that has to tell a
frozen copy from a quiet one compares that timestamp against `resync_seconds`. Every kind is
seeded once at startup for the same reason: a process that never completes its first cycle has to
go stale like any other rather than read as fresh for having nothing to be late against.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from kubernetes_asyncio import watch as k8s_watch
from tenacity import AsyncRetrying, before_sleep_log, wait_exponential

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchedKind:
    """One kind to keep in sync: how to list it, and how to fold one object (or its deletion) in.

    `apply` and `names` are bound to the caller's store; `name` keys the freshness timestamp and
    names the task, so it is the resource's plural.
    """

    name: str
    list: Callable[..., Awaitable[Any]]
    args: tuple[Any, ...]
    parse: Callable[[Any], tuple[str, Any]]
    names: Callable[[], set[str]]
    apply: Callable[[str, Any | None], None]
    kwargs: Mapping[str, Any] = field(default_factory=dict)


class ListWatch:
    def __init__(
        self,
        *,
        kinds: Sequence[WatchedKind],
        resync_seconds: int,
        on_change: Callable[[WatchedKind], Awaitable[None]],
        on_cycle: Callable[[WatchedKind, datetime], Awaitable[None]],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._kinds = tuple(kinds)
        self._resync_seconds = resync_seconds
        self._on_change = on_change
        self._on_cycle = on_cycle
        self._clock = clock
        self._listed: set[str] = set()

    @property
    def synced(self) -> bool:
        """Whether every kind has been listed at least once, so the store is complete."""
        return len(self._listed) == len(self._kinds)

    async def run(self) -> None:
        """Watch until cancelled."""
        started = self._clock()
        for kind in self._kinds:
            await self._on_cycle(kind, started)
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
        parsed = dict(kind.parse(item) for item in items)
        for name in kind.names() - set(parsed):
            kind.apply(name, None)
        for name, obj in parsed.items():
            kind.apply(name, obj)
        self._listed.add(kind.name)
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
                        name, obj = kind.parse(event["object"])
                        kind.apply(name, obj)
                    case "DELETED":
                        name, _ = kind.parse(event["object"])
                        kind.apply(name, None)
                    case "BOOKMARK":
                        continue
                    case other:
                        raise RuntimeError(f"watch of {kind.name} returned event type {other!r}")
                await self._on_change(kind)
        finally:
            watcher.stop()
        # Only here, past the watch the server ended on schedule: a cycle that raised out of the
        # loop above is exactly the case this timestamp is meant to stop advancing for.
        await self._on_cycle(kind, self._clock())


def apply_to[T](store: dict[str, T], name: str, obj: T | None) -> None:
    """The usual `apply`: an object replaces what is stored under its name, and `None` removes it."""
    if obj is None:
        store.pop(name, None)
    else:
        store[name] = obj
