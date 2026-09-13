"""pytest-asyncio configuration for the proxy runtime tests."""

from __future__ import annotations

from cluster.proxies.github_api_proxy.destinations import OriginLoop


def pytest_asyncio_loop_factories(config: object, item: object) -> dict[str, type[OriginLoop]]:
    del config, item
    return {"origin": OriginLoop}
