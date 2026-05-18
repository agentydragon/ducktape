#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.12"
# dependencies = ["telethon", "tqdm"]
# ///

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import TakeoutInitDelayError
from telethon.tl import functions
from tqdm import tqdm
from tqdm.asyncio import tqdm as atqdm

_MEDIA_PARALLELISM = 5


def _json_default(obj: object) -> object:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.hex()
    return str(obj)


async def _download_one(client: TelegramClient, msg, media_dir: Path, sem: asyncio.Semaphore) -> str | None:
    async with sem:
        path = await client.download_media(msg, file=media_dir)
        return Path(path).name if path else None


async def _export(client: TelegramClient, takeout: TelegramClient, entity: str, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    media_dir = out_dir / "media"
    media_dir.mkdir(exist_ok=True)

    entity_info = await client.get_entity(entity)
    (out_dir / "entity.json").write_text(
        json.dumps(entity_info.to_dict(), default=_json_default, indent=2, ensure_ascii=False)
    )

    total = (await takeout.get_messages(entity, limit=0)).total
    sem = asyncio.Semaphore(_MEDIA_PARALLELISM)
    media_tasks: list[tuple[dict, asyncio.Task[str | None]]] = []
    messages: list[dict] = []

    async for msg in atqdm(takeout.iter_messages(entity, wait_time=0), total=total, desc=entity, unit="msg"):
        msg_data = msg.to_dict()
        if msg.media:
            task = asyncio.create_task(_download_one(client, msg, media_dir, sem))
            media_tasks.append((msg_data, task))
        messages.append(msg_data)

    if media_tasks:
        with tqdm(total=len(media_tasks), desc=f"{entity} media", unit="file") as bar:
            for msg_data, task in media_tasks:
                name = await task
                if name:
                    msg_data["_media_file"] = name
                bar.update(1)

    (out_dir / "messages.json").write_text(json.dumps(messages, default=_json_default, indent=2, ensure_ascii=False))


async def _abort_dangling_takeout(client: TelegramClient) -> None:
    tid = client.session.takeout_id
    if tid is None:
        return
    if isinstance(tid, int):
        await client(
            functions.InvokeWithTakeoutRequest(tid, functions.account.FinishTakeoutSessionRequest(success=False))
        )
    client.session.takeout_id = None


async def async_main(args: argparse.Namespace) -> None:
    client = TelegramClient(args.session, int(args.api_id), args.api_hash)
    await client.start()
    try:
        await _abort_dangling_takeout(client)
        async with client.takeout(
            contacts=False, users=True, chats=True, megagroups=True, channels=True, files=True, max_file_size=2**31
        ) as takeout:
            for entity in args.entities:
                await _export(client, takeout, entity, args.output / entity.lstrip("@"))
    except TakeoutInitDelayError as e:
        print(f"error: takeout rate limited, retry in {e.seconds}s", file=sys.stderr)
        sys.exit(1)
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backup Telegram chats via Takeout API")
    parser.add_argument("--api-id", required=True)
    parser.add_argument("--api-hash", required=True)
    parser.add_argument("--session", default="session")
    parser.add_argument("--output", type=Path, default=Path("backup"))
    parser.add_argument("entities", nargs="+", help="Usernames (@foo) or numeric IDs")
    asyncio.run(async_main(parser.parse_args()))


if __name__ == "__main__":
    main()
