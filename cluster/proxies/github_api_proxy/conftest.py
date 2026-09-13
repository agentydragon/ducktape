"""pytest-asyncio configuration for the proxy runtime tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from cluster.proxies.github_api_proxy.destinations import OriginLoop


def origin_loop() -> OriginLoop:
    return OriginLoop("localhost")


def pytest_asyncio_loop_factories(config: object, item: object) -> dict[str, Callable[[], asyncio.AbstractEventLoop]]:
    del config, item
    return {"origin": origin_loop}
