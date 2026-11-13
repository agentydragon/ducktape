"""Export Session evidence independently of which turns a scenario finished observing."""

import asyncio
import json

from google.protobuf.json_format import MessageToDict

from agentplane.app.testing.client import Client
from agentplane.app.testing.history_reads import SessionEventReader
from devinfra.testing.undeclared_outputs import undeclared_outputs_dir


async def capture(client: Client, *, sandbox_uid: str) -> None:
    destination = undeclared_outputs_dir() / "vm-artifacts" / "native" / sandbox_uid
    destination.mkdir(parents=True, exist_ok=True)
    async with asyncio.timeout(30):
        async for thread in client.threads(sandbox_uid=sandbox_uid):
            (destination / f"{thread.id}.json").write_text(thread.model_dump_json(indent=2) + "\n")
            watermark = (await client.session_summary(thread.id)).committed_cursor
            if watermark == 0:
                continue
            reader = SessionEventReader(client, thread.id, after_cursor=0)
            with (destination / f"{thread.id}.jsonl").open("w") as output:
                while reader.cursor < watermark:
                    await reader.read_page()
                    output.seek(0)
                    output.truncate()
                    for entry in reader.entries:
                        if entry.cursor > watermark:
                            break
                        # Keep stderr and lifecycle failures alongside frames so early exits are diagnosable.
                        output.write(json.dumps(MessageToDict(entry)) + "\n")
                    output.flush()
                    if reader.cursor < watermark:
                        await reader.wait_for_next_page()
