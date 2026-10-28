"""Loopback file executor for the sampler experiment; launch in a separate process."""

import argparse
import asyncio
import os
from contextlib import AsyncExitStack
from pathlib import Path

from aiohttp import web
from pydantic import BaseModel, ConfigDict


class Append(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    text: str


def create_app(workspace: Path) -> web.Application:
    workspace = workspace.resolve()

    def perform(operation: Append) -> None:
        target = (workspace / operation.path).resolve()
        if Path(operation.path).is_absolute() or not target.is_relative_to(workspace):
            raise web.HTTPForbidden(text="path escapes executor workspace")
        # A convenience check for the fixture, not a defense against concurrent symlink replacement.
        with target.open("a", encoding="utf-8") as file:
            file.write(operation.text)

    async def append(request: web.Request) -> web.Response:
        operation = Append.model_validate(await request.json())
        await asyncio.to_thread(perform, operation)
        return web.json_response({"appended": len(operation.text.encode()), "executor_pid": os.getpid()})

    app = web.Application(client_max_size=64 * 1024)
    app.router.add_post("/append", append)
    return app


async def serve(workspace: Path) -> None:
    async with AsyncExitStack() as stack:
        runner = web.AppRunner(create_app(workspace))
        await runner.setup()
        stack.push_async_callback(runner.cleanup)
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        print(f"http://127.0.0.1:{runner.addresses[0][1]}/append", flush=True)
        await asyncio.Event().wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(serve(args.workspace))


if __name__ == "__main__":
    main()
