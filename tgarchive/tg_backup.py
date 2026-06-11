#!/usr/bin/env python3
import argparse
import asyncio
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import yaml
from telethon import TelegramClient
from telethon.errors import TakeoutInitDelayError
from telethon.tl import functions
from telethon.tl.custom import Message
from telethon.tl.types import Channel, Chat, User
from tqdm import tqdm
from tqdm.asyncio import tqdm as atqdm

# Concrete entity types `client.get_entity()` / `dialog.entity` may return.
type Entity = User | Chat | Channel

_MEDIA_PARALLELISM = 5
_DEFAULT_SECRET = Path(__file__).resolve().parent.parent / "secrets/shared/telegram-api.yaml"


def _load_credentials(secret_path: Path) -> tuple[int, str]:
    plaintext = subprocess.run(["sops", "-d", str(secret_path)], check=True, capture_output=True, text=True).stdout
    data = yaml.safe_load(plaintext)
    return int(data["api_id"]), data["api_hash"]


def _json_default(obj: object) -> object:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, bytes):
        return obj.hex()
    return str(obj)


async def _resolve_entity(client: TelegramClient, name: str) -> Entity:
    # Try direct lookup first (handles @username, numeric IDs, t.me/... links).
    try:
        return await client.get_entity(name)
    except (ValueError, TypeError):
        pass
    matches: list[Entity] = [d.entity async for d in client.iter_dialogs() if d.title == name]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(f"no entity matching {name!r} (tried username/ID and dialog titles)")
    raise ValueError(f"{len(matches)} dialogs match title {name!r}; use a username or ID instead")


def _entity_slug(entity: Entity) -> str:
    if isinstance(entity, (User, Channel)) and entity.username:
        return entity.username
    label = entity.first_name if isinstance(entity, User) else entity.title
    if not label:
        return str(entity.id)
    return re.sub(r"[^\w-]+", "_", label).strip("_") or str(entity.id)


def _filename_for(msg: Message) -> str:
    # `msg.id` prefix guarantees uniqueness across the export; the original
    # document name (when present) is preserved for human readability.
    f = msg.file
    if f.name:
        return f"{msg.id}_{f.name}"
    return f"{msg.id}{f.ext or ''}"


async def _download_one(client: TelegramClient, msg: Message, dest: Path, sem: asyncio.Semaphore) -> None:
    async with sem:
        await client.download_media(msg, file=dest)


def _prepare_export_dir(out_dir: Path, entity: Entity) -> Path:
    """Create the export + media dirs and write entity.json; return the media dir.

    Synchronous on purpose: run via `asyncio.to_thread` from the async exporter so the blocking
    filesystem setup doesn't stall the event loop (ASYNC240).
    """
    media_dir = out_dir / "media"
    media_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "entity.json").write_text(
        json.dumps(entity.to_dict(), default=_json_default, indent=2, ensure_ascii=False)
    )
    return media_dir


async def _export(takeout: TelegramClient, client: TelegramClient, entity: Entity, out_dir: Path) -> None:
    media_dir = await asyncio.to_thread(_prepare_export_dir, out_dir, entity)

    desc = _entity_slug(entity)
    total = (await takeout.get_messages(entity, limit=0)).total
    sem = asyncio.Semaphore(_MEDIA_PARALLELISM)
    media_tasks: list[asyncio.Task[None]] = []
    messages: list[dict] = []

    async for msg in atqdm(takeout.iter_messages(entity, wait_time=0), total=total, desc=desc, unit="msg"):
        msg_data = msg.to_dict()
        if msg.file is not None:
            fname = _filename_for(msg)
            msg_data["_media_file"] = fname
            media_tasks.append(asyncio.create_task(_download_one(client, msg, media_dir / fname, sem)))
        messages.append(msg_data)

    if media_tasks:
        with tqdm(total=len(media_tasks), desc=f"{desc} media", unit="file") as bar:
            for task in asyncio.as_completed(media_tasks):
                await task
                bar.update(1)

    await asyncio.to_thread(
        (out_dir / "messages.json").write_text,
        json.dumps(messages, default=_json_default, indent=2, ensure_ascii=False),
    )


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
    api_id, api_hash = _load_credentials(args.secret)
    client = TelegramClient(args.session, api_id, api_hash)
    await client.start()
    try:
        await _abort_dangling_takeout(client)
        resolved = [await _resolve_entity(client, name) for name in args.entities]
        async with client.takeout(
            contacts=False, users=True, chats=True, megagroups=True, channels=True, files=True, max_file_size=2**31
        ) as takeout:
            for entity in resolved:
                await _export(takeout, client, entity, args.output / _entity_slug(entity))
    except TakeoutInitDelayError as e:
        print(f"error: takeout rate limited, retry in {e.seconds}s", file=sys.stderr)
        sys.exit(1)
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backup Telegram chats via Takeout API")
    parser.add_argument("--secret", type=Path, default=_DEFAULT_SECRET, help="Path to SOPS-encrypted telegram-api.yaml")
    parser.add_argument("--session", default="session")
    parser.add_argument("--output", type=Path, default=Path("backup"))
    parser.add_argument(
        "entities",
        nargs="+",
        help="@usernames, numeric IDs, t.me/... links, or exact dialog titles (groups/channels/users)",
    )
    asyncio.run(async_main(parser.parse_args()))


if __name__ == "__main__":
    main()
