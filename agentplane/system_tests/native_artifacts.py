"""Export Session evidence independently of which turns a scenario finished observing."""

import asyncio
import json
from contextlib import aclosing
from uuid import UUID

from google.protobuf.json_format import MessageToDict

from agentplane.app.testing.client import Client
from util.testing.undeclared_outputs import undeclared_outputs_dir


async def capture(client: Client, *, sandbox_uid: UUID) -> None:
    destination = undeclared_outputs_dir() / "vm-artifacts" / "native" / str(sandbox_uid)
    destination.mkdir(parents=True, exist_ok=True)
    async with asyncio.timeout(30), aclosing(client.threads(sandbox_uid=sandbox_uid)) as threads:
        async for thread in threads:
            (destination / f"{thread.id}.json").write_text(thread.model_dump_json(indent=2) + "\n")
            if thread.last_cursor == 0:
                continue
            cursor = 0
            with (destination / f"{thread.id}.jsonl").open("w") as output:
                # An idle active feed never ends. Stop at the archive watermark we just listed.
                async with aclosing(client.events(thread.id, after=0, read_seconds=30)) as entries:
                    async for entry in entries:
                        cursor = entry.cursor
                        # Keep stderr and lifecycle failures alongside frames so early exits are diagnosable.
                        output.write(json.dumps(MessageToDict(entry)) + "\n")
                        output.flush()
                        if cursor >= thread.last_cursor:
                            break
            if cursor < thread.last_cursor:
                raise RuntimeError(f"Thread {thread.id} ended at {cursor}, before archive cursor {thread.last_cursor}")
