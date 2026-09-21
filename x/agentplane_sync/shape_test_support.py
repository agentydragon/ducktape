"""Helpers shared by the Electric shape capacity and history-memory probes."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, cast

import asyncpg
import httpx

from util.testing.container_logs import LoggedContainer

SMALL_ROW_COUNT = 1_000
LARGE_ROW_COUNT = 20_000
SHAPE_LIMIT = 2
SHAPE_COLUMNS = "conversation_id,row_key,anchor"
ACTIVE_SHAPE_COLUMNS = f"{SHAPE_COLUMNS},revision"
BOUNDED_HISTORY_CONVERSATION = "bounded-history"
BOUNDED_HISTORY_TAIL_ANCHOR = 9_007_199_254_740_993
BOUNDED_HISTORY_TAIL_ROWS = 30
BOUNDED_HISTORY_BATCH_ROWS = 10_000


def _write_evidence(path: Path, evidence: dict[str, Any]) -> None:
    path.write_text(json.dumps(evidence, indent=2, sort_keys=True) + "\n")


def _messages(body: bytes) -> list[dict[str, Any]]:
    if not body:
        return []
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        result: list[dict[str, Any]] = []
        for line in body.splitlines():
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            result.extend(parsed if isinstance(parsed, list) else [parsed])
        return result
    if isinstance(decoded, list):
        return cast(list[dict[str, Any]], decoded)
    if isinstance(decoded, dict) and isinstance(decoded.get("data"), list):
        return cast(list[dict[str, Any]], decoded["data"])
    return [decoded]


async def _connect_postgres(dsn: str) -> asyncpg.Pool:
    last_error: BaseException | None = None
    for _ in range(120):
        try:
            connection = await asyncpg.connect(dsn, timeout=2)
            await connection.close()
            return await asyncpg.create_pool(dsn, min_size=1, max_size=8)
        except (asyncpg.PostgresError, OSError, TimeoutError) as error:
            last_error = error
            await asyncio.sleep(0.1)
    raise TimeoutError("PostgreSQL did not accept connections") from last_error


async def _wait_electric(url: str) -> dict[str, Any]:
    last_error: BaseException | None = None
    async with httpx.AsyncClient(timeout=2) as client:
        for _ in range(180):
            try:
                response = await client.get(f"{url}/v1/health")
                if response.status_code == 200 and response.json().get("status") == "active":
                    return cast(dict[str, Any], response.json())
            except httpx.HTTPError as error:
                last_error = error
            await asyncio.sleep(0.1)
    raise TimeoutError("Electric 1.8.1 did not reach /v1/health active") from last_error


async def _seed_history(pool: asyncpg.Pool, conversation_id: str, count: int) -> None:
    await pool.execute(
        """INSERT INTO sync_view_row (conversation_id,row_key,entity_kind,anchor,revision)
           SELECT $1, $1 || '-item-' || i::text, 'item', i::bigint, i::bigint
           FROM generate_series(1, $2::int) AS series(i)""",
        conversation_id,
        count,
    )


def _sample_memory(electric: LoggedContainer) -> dict[str, Any]:
    container = electric.get_wrapped_container()
    stats = container.stats(stream=False)
    memory = stats.get("memory_stats", {})
    memory_stats = memory.get("stats", {})
    sample: dict[str, Any] = {
        "cgroupUsageBytes": memory.get("usage"),
        "cgroupLimitBytes": memory.get("limit"),
        "cgroupStats": memory_stats,
    }
    proc_status = container.exec_run(["cat", "/proc/1/status"])
    sample["pid1StatusExitCode"] = proc_status.exit_code
    if proc_status.exit_code == 0:
        status = proc_status.output.decode(errors="replace")
        sample["pid1VmRSSKiB"] = _proc_status_value(status, "VmRSS")
        sample["pid1VmHWMKiB"] = _proc_status_value(status, "VmHWM")
        cmdline = container.exec_run(["cat", "/proc/1/cmdline"])
        sample["pid1Command"] = cmdline.output.decode(errors="replace").replace("\x00", " ").strip()
    return sample


def _proc_status_value(status: str, key: str) -> int | None:
    match = re.search(rf"^{re.escape(key)}:\s+(\d+)\s+kB$", status, re.MULTILINE)
    return int(match.group(1)) if match else None


def _shape_params(
    conversation_id: str, *, where_clause: str | None = None, columns: str = SHAPE_COLUMNS, live: str = "false"
) -> dict[str, str]:
    return {
        "table": "sync_view_row",
        "where": where_clause or f"conversation_id = '{conversation_id}'",
        "columns": columns,
        "replica": "full",
        "log": "full",
        "live": live,
    }


async def _read_snapshot(
    client: httpx.AsyncClient,
    shape_url: str,
    conversation_id: str,
    *,
    where_clause: str | None = None,
    columns: str = SHAPE_COLUMNS,
) -> dict[str, Any]:
    base_params = _shape_params(conversation_id, where_clause=where_clause, columns=columns)
    offset = "-1"
    handle: str | None = None
    all_messages: list[dict[str, Any]] = []
    pages: list[dict[str, Any]] = []
    for page_number in range(100):
        params = {**base_params, "offset": offset}
        if handle is not None:
            params["handle"] = handle
        response = await client.get(shape_url, params=params)
        assert response.status_code == 200, {
            "conversationId": conversation_id,
            "status": response.status_code,
            "headers": dict(response.headers),
            "body": response.content[:2000].decode(errors="replace"),
        }
        if handle is None:
            handle = response.headers.get("electric-handle")
            assert handle, {"conversationId": conversation_id, "headers": dict(response.headers)}
        next_offset = response.headers.get("electric-offset")
        messages = _messages(response.content)
        all_messages.extend(messages)
        controls = [message.get("headers", {}).get("control") for message in messages]
        pages.append(
            {
                "page": page_number,
                "requestOffset": offset,
                "responseOffset": next_offset,
                "status": response.status_code,
                "rowCount": sum(1 for message in messages if message.get("headers", {}).get("operation")),
                "controlMessages": [control for control in controls if control],
                "responseBytes": len(response.content),
            }
        )
        if "up-to-date" in controls:
            assert next_offset, {"conversationId": conversation_id, "pages": pages}
            return {
                "conversationId": conversation_id,
                "handle": handle,
                "offset": next_offset,
                "rows": [message["value"] for message in all_messages if message.get("headers", {}).get("operation")],
                "pages": pages,
                "rowCount": sum(page["rowCount"] for page in pages),
                "responseBytes": sum(page["responseBytes"] for page in pages),
            }
        assert next_offset, {"conversationId": conversation_id, "offset": offset, "pages": pages}
        assert next_offset != offset, {
            "conversationId": conversation_id,
            "offset": offset,
            "nextOffset": next_offset,
            "pages": pages,
        }
        offset = next_offset
    raise AssertionError(
        {"conversationId": conversation_id, "message": "shape snapshot did not reach up-to-date", "pages": pages}
    )


async def _read_stale_handle(
    client: httpx.AsyncClient,
    shape_url: str,
    snapshot: dict[str, Any],
    *,
    where_clause: str | None = None,
    columns: str = SHAPE_COLUMNS,
) -> httpx.Response:
    params = {
        **_shape_params(snapshot["conversationId"], where_clause=where_clause, columns=columns),
        "handle": snapshot["handle"],
        "offset": snapshot["offset"],
    }
    return await client.get(shape_url, params=params)


async def _read_live_page(
    client: httpx.AsyncClient, shape_url: str, snapshot: dict[str, Any], *, offset: str, where_clause: str, columns: str
) -> dict[str, Any]:
    params = {
        **_shape_params(snapshot["conversationId"], where_clause=where_clause, columns=columns, live="true"),
        "handle": snapshot["handle"],
        "offset": offset,
    }
    response = await client.get(shape_url, params=params)
    assert response.status_code == 200, {
        "conversationId": snapshot["conversationId"],
        "handle": snapshot["handle"],
        "offset": offset,
        "status": response.status_code,
        "headers": dict(response.headers),
        "body": response.content[:2000].decode(errors="replace"),
    }
    messages = _messages(response.content)
    event_offsets = [
        message.get("headers", {}).get("offset") for message in messages if message.get("headers", {}).get("operation")
    ]
    return {
        "status": response.status_code,
        "headers": dict(response.headers),
        "responseBytes": len(response.content),
        "offset": response.headers.get("electric-offset") or (event_offsets[-1] if event_offsets else None),
        "messages": messages,
        "operations": [message for message in messages if message.get("headers", {}).get("operation")],
    }


async def _seed_bounded_history(pool: asyncpg.Pool) -> None:
    await pool.execute(
        """INSERT INTO sync_view_row (conversation_id,row_key,entity_kind,anchor,revision)
           SELECT $1, $1 || '-old-' || i::text, 'item', i::bigint, i::bigint
           FROM generate_series(1, 70) AS series(i)""",
        BOUNDED_HISTORY_CONVERSATION,
    )
    await pool.execute(
        """INSERT INTO sync_view_row (conversation_id,row_key,entity_kind,anchor,revision)
           SELECT $1, $1 || '-tail-' || i::text, 'item', $2::bigint + i::bigint, $2::bigint + i::bigint
           FROM generate_series(0, $3::int - 1) AS series(i)""",
        BOUNDED_HISTORY_CONVERSATION,
        BOUNDED_HISTORY_TAIL_ANCHOR,
        BOUNDED_HISTORY_TAIL_ROWS,
    )


async def _insert_older_history_rows(pool: asyncpg.Pool, first: int, last: int) -> list[dict[str, int]]:
    batches: list[dict[str, int]] = []
    for batch_first in range(first, last + 1, BOUNDED_HISTORY_BATCH_ROWS):
        batch_last = min(batch_first + BOUNDED_HISTORY_BATCH_ROWS - 1, last)
        await pool.execute(
            """INSERT INTO sync_view_row (conversation_id,row_key,entity_kind,anchor,revision)
               SELECT $1, $1 || '-old-' || i::text, 'item', i::bigint, i::bigint
               FROM generate_series($2::int, $3::int) AS series(i)""",
            BOUNDED_HISTORY_CONVERSATION,
            batch_first,
            batch_last,
        )
        batches.append(
            {"firstOldAnchor": batch_first, "lastOldAnchor": batch_last, "rows": batch_last - batch_first + 1}
        )
    return batches


async def _seed_interest_window(pool: asyncpg.Pool, conversation_id: str) -> None:
    await pool.execute(
        """INSERT INTO sync_view_row (conversation_id,row_key,entity_kind,anchor,revision)
           SELECT $1, $1 || '-item-' || i::text, 'item', i::bigint, i::bigint
           FROM generate_series(1, $2::int) AS series(i)""",
        conversation_id,
        BOUNDED_HISTORY_TAIL_ROWS,
    )
