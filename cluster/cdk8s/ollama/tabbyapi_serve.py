"""Run the pinned TabbyAPI app with a request lock for its single generation slot."""

import asyncio
from collections.abc import Awaitable, Callable
from importlib import import_module
from typing import Protocol, cast

from starlette.applications import Starlette
from starlette.types import ASGIApp, Receive, Scope, Send

_MODEL_REQUESTS = frozenset(
    {
        "/v1/chat/completions",
        "/v1/completions",
        "/v1/continuations",
        "/v1/decisions",
        "/v1/systemone",
        "/v1/model/load",
        "/v1/model/unload",
        "/apply-template",
        "/v1/apply-template",
    }
)


class SerializeModelRequests:
    """Protect the shared model from template rendering through the end of streaming."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.lock = asyncio.Lock()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope["path"].rstrip("/") not in _MODEL_REQUESTS:
            await self.app(scope, receive, send)
            return

        # TabbyAPI's router releases its load lock before generation registers
        # a job. Unload waits for registered jobs only. Hold this lock over the
        # ASGI call so it also covers SSE iteration and disconnect cleanup.
        async with self.lock:
            await self.app(scope, receive, send)


class _TabbyMain(Protocol):
    entrypoint_async: Callable[[], Awaitable[None]]

    def entrypoint(self) -> None: ...


class _TabbyServer(Protocol):
    setup_app: Callable[[str | None, int | None], Starlette]


def main() -> None:
    tabby = cast(_TabbyMain, import_module("main"))
    entrypoint_async = tabby.entrypoint_async

    async def serve() -> None:
        # Import here so TabbyAPI first configures the PyTorch CUDA allocator.
        server = cast(_TabbyServer, import_module("endpoints.server"))
        setup_app = server.setup_app

        def setup_locked_app(host: str | None = None, port: int | None = None) -> Starlette:
            app = setup_app(host, port)
            app.add_middleware(SerializeModelRequests)
            return app

        server.setup_app = setup_locked_app
        await entrypoint_async()

    tabby.entrypoint_async = serve
    tabby.entrypoint()


if __name__ == "__main__":
    main()
