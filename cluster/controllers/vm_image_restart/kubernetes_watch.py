"""Keep the opted-in KubeVirt VM and VMI in sync with the API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from kubernetes_asyncio import client, watch
from tenacity import before_sleep_log, retry, wait_exponential

logger = logging.getLogger(__name__)

WATCH_TIMEOUT_SECONDS = 300


async def watch_kubevirt_resource(
    api: client.CustomObjectsApi,
    namespace: str,
    plural: str,
    name: str,
    store: dict[str, dict[str, Any]],
    changed: asyncio.Event,
) -> None:
    """Relist after each watch and retry failed cycles with backoff."""
    list_objects = api.list_namespaced_custom_object
    args = ("kubevirt.io", "v1", namespace, plural)
    field_selector = f"metadata.name={name}"
    request_timeout = (10, WATCH_TIMEOUT_SECONDS + 30)

    @retry(wait=wait_exponential(max=30), before_sleep=before_sleep_log(logger, logging.WARNING))
    async def cycle() -> None:
        listed = await list_objects(*args, field_selector=field_selector, _request_timeout=request_timeout)
        resource_version = listed["metadata"]["resourceVersion"]
        current = {str(obj["metadata"]["name"]): obj for obj in listed["items"]}
        store.clear()
        store.update(current)
        changed.set()

        watcher = watch.Watch()
        try:
            # timeoutSeconds must be an integer or the API server refuses the watch.
            async for event in watcher.stream(
                list_objects,
                *args,
                field_selector=field_selector,
                _request_timeout=request_timeout,
                resource_version=resource_version,
                timeout_seconds=WATCH_TIMEOUT_SECONDS,
            ):
                match event["type"]:
                    case "ADDED" | "MODIFIED":
                        obj = event["object"]
                        store[str(obj["metadata"]["name"])] = obj
                    case "DELETED":
                        store.pop(str(event["object"]["metadata"]["name"]), None)
                    case "BOOKMARK":
                        continue
                    case other:
                        raise RuntimeError(f"watch of {plural} returned event type {other!r}")
                changed.set()
        finally:
            watcher.stop()

    while True:
        await cycle()
