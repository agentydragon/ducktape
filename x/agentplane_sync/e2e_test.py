"""Real Postgres, Electric, Python proxy, TanStack DB, React, and Chromium experiment."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import parse_qs, urlsplit, urlunsplit

import asyncpg
import httpx
import pytest
import pytest_bazel
import uvicorn
from playwright.async_api import BrowserContext, Page, Request, Response, async_playwright
from testcontainers.core.network import Network

from agentplane.protocol import event_log_pb2, event_pb2
from third_party.containers import electric_1_8, postgres_18, ryuk
from util.bazel.runfiles import get_required_path
from util.net import bind_free_port
from util.oci import load_oci_image
from util.testing.container_logs import LoggedContainer
from util.testing.frontend_visual import CONTAINER_BASE_BROWSER_ARGS, chromium_executable
from util.testing.undeclared_outputs import undeclared_outputs_dir
from x.agentplane_sync.projector import (
    ApplyResult,
    UnknownItemError,
    apply_batch,
    initialize_database,
    payload_reference,
)
from x.agentplane_sync.service import PayloadGate, SubsetGate, create_electric_proxy, create_gateway

if TYPE_CHECKING:
    from google.protobuf.message import Message

SOURCE = "runner-alpha-large"
HIGH_CURSOR = 9_007_199_254_740_993
SMALL_COUNT = 1_000
LARGE_COUNT = 50_000
TOKEN = "derisk-test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
SENSITIVE = (
    "SENSITIVE_INITIAL_OUTPUT",
    "SENSITIVE_INITIAL_REASONING",
    "SENSITIVE_OUTPUT_AFTER",
    "SENSITIVE_OUTPUT_CLOSED",
)


def _entry(source_id: str, cursor: int, field: str, value: Message) -> event_log_pb2.EventEntry:
    entry = event_log_pb2.EventEntry(
        cursor=cursor, origin=event_log_pb2.EventOrigin(source_id=source_id, sequence=cursor)
    )
    getattr(entry.event, field).CopyFrom(value)
    return entry


def _text_entry(source: str, cursor: int, item_id: str, text: str) -> event_log_pb2.EventEntry:
    return _entry(source, cursor, "text_delta", event_pb2.TextDelta(item_id=item_id, text=text))


async def _seed_conversation(pool: asyncpg.Pool, conversation_id: str, *, count: int, first_anchor: int) -> None:
    await pool.execute(
        """INSERT INTO sync_view_row
             (conversation_id,row_key,entity_kind,anchor,revision,item_id,item_kind,status)
           SELECT $1,
                  'item:' || $1 || '-seed-' || i::text,
                  'item',
                  $2::bigint + i,
                  $2::bigint + i,
                  $1 || '-seed-' || i::text,
                  $4,
                  'complete'
           FROM generate_series(1, $3::int) AS series(i)""",
        conversation_id,
        first_anchor,
        count,
        event_pb2.ITEM_KIND_ASSISTANT_TEXT,
    )


def _payload_seed_records(
    conversation_id: str,
    item_id: str,
    field_name: str,
    source_id: str,
    revision: int,
    generation_id: str,
    content_chunks: list[str],
) -> tuple[tuple[Any, ...], list[tuple[Any, ...]], str]:
    chunks = [(index, value, len(value.encode("utf-8"))) for index, value in enumerate(content_chunks) if value]
    content_bytes = sum(byte_count for _, _, byte_count in chunks)
    reference = payload_reference(conversation_id, item_id, field_name, source_id, generation_id, revision)
    manifest = (
        reference,
        conversation_id,
        item_id,
        field_name,
        source_id,
        generation_id,
        revision,
        True,
        len(chunks),
        content_bytes,
        revision,
    )
    chunk_rows = [
        (conversation_id, item_id, field_name, source_id, generation_id, index, revision, value, byte_count)
        for index, value, byte_count in chunks
    ]
    return manifest, chunk_rows, reference


async def _seed_payload(
    pool: asyncpg.Pool,
    conversation_id: str,
    item_id: str,
    field_name: str,
    source_id: str,
    revision: int,
    generation_id: str,
    content_chunks: list[str],
) -> tuple[str, int, int]:
    manifest, chunks, reference = _payload_seed_records(
        conversation_id, item_id, field_name, source_id, revision, generation_id, content_chunks
    )
    await pool.execute(
        """INSERT INTO projected_payload_manifest
           (payload_ref,conversation_id,item_id,field_name,source_id,generation_id,revision,present,
            chunk_count,content_bytes,source_cursor)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
        *manifest,
    )
    if chunks:
        await pool.executemany(
            """INSERT INTO projected_payload_chunk
               (conversation_id,item_id,field_name,source_id,generation_id,chunk_index,source_cursor,content,content_bytes)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
            chunks,
        )
    return reference, int(manifest[8]), int(manifest[9])


async def _seed_payload_probes(pool: asyncpg.Pool) -> dict[str, dict[str, Any]]:
    manifest_rows: list[tuple[Any, ...]] = []
    chunk_rows: list[tuple[Any, ...]] = []
    probe_values: dict[str, dict[str, Any]] = {}
    for name, history_count, current_chunks, anchor in (
        ("payload-small", 12, ["small:", "value"], HIGH_CURSOR + 10),
        ("payload-large", 5_000, ["L" * 128 for _ in range(32)], HIGH_CURSOR + 11),
    ):
        item_id = name
        source_id = f"fixture-{name}"
        for revision in range(1, history_count + 1):
            manifest, chunks, _ = _payload_seed_records(
                "alpha-small",
                item_id,
                "text",
                source_id,
                revision,
                f"superseded-{revision}",
                [f"superseded body {revision}"],
            )
            manifest_rows.append(manifest)
            chunk_rows.extend(chunks)
        generation_id = "current-generation"
        manifest, chunks, reference = _payload_seed_records(
            "alpha-small", item_id, "text", source_id, anchor, generation_id, current_chunks
        )
        manifest_rows.append(manifest)
        chunk_rows.extend(chunks)
        content_bytes = int(manifest[9])
        probe_values[name] = {
            "itemId": item_id,
            "fieldName": "text",
            "sourceId": source_id,
            "generationId": generation_id,
            "payloadRef": reference,
            "anchor": anchor,
            "content": "".join(current_chunks),
            "contentBytes": content_bytes,
            "chunkCount": int(manifest[8]),
            "historyChunks": history_count,
        }
    await pool.executemany(
        """INSERT INTO projected_payload_manifest
           (payload_ref,conversation_id,item_id,field_name,source_id,generation_id,revision,present,
            chunk_count,content_bytes,source_cursor)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)""",
        manifest_rows,
    )
    await pool.executemany(
        """INSERT INTO projected_payload_chunk
           (conversation_id,item_id,field_name,source_id,generation_id,chunk_index,source_cursor,content,content_bytes)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
        chunk_rows,
    )
    for name, probe in probe_values.items():
        await pool.execute(
            """INSERT INTO sync_view_row
               (conversation_id,row_key,entity_kind,anchor,revision,item_id,item_kind,text_revision,text_bytes,
                text_payload_ref,text_generation_id,text_chunk_count,status)
               VALUES ('alpha-small',$1,'item',$2,$2,$3,$4,$2,$5,$6,$7,$8,'streaming')""",
            f"item:{name}",
            probe["anchor"],
            name,
            event_pb2.ITEM_KIND_ASSISTANT_TEXT,
            probe["contentBytes"],
            probe["payloadRef"],
            probe["generationId"],
            probe["chunkCount"],
        )
    return probe_values


async def _seed_large_live_rows(pool: asyncpg.Pool) -> int:
    base = HIGH_CURSOR + LARGE_COUNT + 100
    text_ref, text_count, _ = await _seed_payload(
        pool, "alpha-large", "live-item", "text", SOURCE, base - 4, "seed-live-text", ["seed text"]
    )
    arguments_ref, arguments_count, _ = await _seed_payload(
        pool, "alpha-large", "tool-row", "arguments", SOURCE, base - 3, "seed-tool-arguments", []
    )
    output_ref, output_count, _ = await _seed_payload(
        pool,
        "alpha-large",
        "tool-row",
        "output",
        SOURCE,
        base - 3,
        "seed-tool-output",
        ["SENSITIVE_INITIAL_OUTPUT"],
    )
    reasoning_ref, reasoning_count, _ = await _seed_payload(
        pool,
        "alpha-large",
        "reasoning-row",
        "reasoning",
        SOURCE,
        base - 2,
        "seed-reasoning",
        ["SENSITIVE_INITIAL_REASONING"],
    )
    older_output_ref, older_output_count, older_output_bytes = await _seed_payload(
        pool,
        "alpha-large",
        "older-tool-row",
        "output",
        SOURCE,
        base - 5,
        "seed-older-output",
        ["older streamed ", "bytes"],
    )
    no_payload = [None, None, None, None]
    no_generation = [None, None, None, None]
    no_chunks = [0, 0, 0, 0]
    rows = [
        (
            "alpha-large",
            "item:live-item",
            "item",
            base - 4,
            base - 4,
            "live-item",
            event_pb2.ITEM_KIND_ASSISTANT_TEXT,
            None,
            base - 4,
            0,
            0,
            0,
            8,
            0,
            0,
            0,
            text_ref,
            None,
            None,
            None,
            "seed-live-text",
            None,
            None,
            None,
            text_count,
            0,
            0,
            0,
            "streaming",
            None,
            None,
        ),
        (
            "alpha-large",
            "item:tool-row",
            "item",
            base - 3,
            base - 3,
            "tool-row",
            event_pb2.ITEM_KIND_TOOL_CALL,
            "read_file",
            0,
            base - 3,
            base - 3,
            0,
            0,
            0,
            24,
            0,
            None,
            arguments_ref,
            output_ref,
            None,
            None,
            "seed-tool-arguments",
            "seed-tool-output",
            None,
            0,
            arguments_count,
            output_count,
            0,
            "streaming",
            None,
            None,
        ),
        (
            "alpha-large",
            "item:reasoning-row",
            "item",
            base - 2,
            base - 2,
            "reasoning-row",
            event_pb2.ITEM_KIND_REASONING,
            None,
            0,
            0,
            0,
            base - 2,
            0,
            0,
            0,
            30,
            None,
            None,
            None,
            reasoning_ref,
            None,
            None,
            None,
            "seed-reasoning",
            0,
            0,
            0,
            reasoning_count,
            "complete",
            None,
            None,
        ),
        (
            "alpha-large",
            "control:model",
            "control",
            base - 1,
            base - 1,
            None,
            None,
            None,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            *no_payload,
            *no_generation,
            *no_chunks,
            "current",
            "model-v1",
            None,
        ),
        (
            "alpha-large",
            "command:sync-command",
            "command",
            base,
            base,
            None,
            None,
            None,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            *no_payload,
            *no_generation,
            *no_chunks,
            "pending",
            None,
            "sync-command",
        ),
        (
            "alpha-large",
            "item:older-tool-row",
            "item",
            base - 5,
            base - 5,
            "older-tool-row",
            event_pb2.ITEM_KIND_TOOL_CALL,
            "read_file",
            0,
            0,
            base - 5,
            0,
            0,
            0,
            older_output_bytes,
            0,
            None,
            None,
            older_output_ref,
            None,
            None,
            None,
            "seed-older-output",
            None,
            0,
            0,
            older_output_count,
            0,
            "streaming",
            None,
            None,
        ),
    ]
    columns = (
        "conversation_id,row_key,entity_kind,anchor,revision,item_id,item_kind,tool_name,"
        "text_revision,arguments_revision,output_revision,reasoning_revision,"
        "text_bytes,arguments_bytes,output_bytes,reasoning_bytes,"
        "text_payload_ref,arguments_payload_ref,output_payload_ref,reasoning_payload_ref,"
        "text_generation_id,arguments_generation_id,output_generation_id,reasoning_generation_id,"
        "text_chunk_count,arguments_chunk_count,output_chunk_count,reasoning_chunk_count,status,model,command_id"
    )
    await pool.executemany(
        f"INSERT INTO sync_view_row ({columns}) VALUES (" + ",".join(f"${index}" for index in range(1, 32)) + ")",
        rows,
    )
    await pool.execute(
        "INSERT INTO projection_checkpoint (conversation_id,source_id,through_cursor) VALUES ('alpha-large',$1,$2)",
        SOURCE,
        base,
    )
    return base


def _messages(body: bytes) -> list[dict[str, Any]]:
    if not body:
        return []
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        result = []
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
        return decoded
    if isinstance(decoded, dict) and isinstance(decoded.get("data"), list):
        return cast(list[dict[str, Any]], decoded["data"])
    return [decoded]


def _parse_body(body: bytes) -> dict[str, Any]:
    try:
        decoded = json.loads(body)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _flatten_plan(node: dict[str, Any]) -> list[dict[str, Any]]:
    descendants = [node]
    for child in node.get("Plans", []):
        descendants.extend(_flatten_plan(child))
    return descendants


async def _query_plan(pool: asyncpg.Pool, conversation_id: str, before: int | None = None) -> Any:
    if before is None:
        raw = await pool.fetchval(
            """EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
               SELECT row_key, anchor, revision
               FROM sync_view_row
               WHERE conversation_id = $1
               ORDER BY anchor DESC, row_key
               LIMIT 30""",
            conversation_id,
        )
    else:
        raw = await pool.fetchval(
            """EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
               SELECT row_key, anchor, revision
               FROM sync_view_row
               WHERE conversation_id = $1 AND anchor < $2
               ORDER BY anchor DESC, row_key
               LIMIT 30""",
            conversation_id,
            before,
        )
    return json.loads(raw) if isinstance(raw, str) else raw


async def _payload_chunk_plan(
    pool: asyncpg.Pool, item: dict[str, Any], *, conversation_id: str = "alpha-small"
) -> Any:
    raw = await pool.fetchval(
        """EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
           SELECT chunk_index, content, content_bytes FROM projected_payload_chunk
           WHERE conversation_id = $1 AND item_id = $2 AND field_name = $3
             AND source_id = $4 AND generation_id = $5
           ORDER BY chunk_index""",
        conversation_id,
        item["itemId"],
        item["fieldName"],
        item["sourceId"],
        item["generationId"],
    )
    return json.loads(raw) if isinstance(raw, str) else raw


async def _check_projector_retry_and_race(pool: asyncpg.Pool) -> dict[str, Any]:
    await pool.execute(
        "INSERT INTO projection_checkpoint (conversation_id,source_id,through_cursor) VALUES ('race-thread','runner-race',0)"
    )
    initial = [
        _entry(
            "runner-race",
            1,
            "item_started",
            event_pb2.ItemStarted(item_id="race-item", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT),
        ),
        _text_entry("runner-race", 2, "race-item", "seed"),
    ]
    parallel = await asyncio.gather(
        apply_batch(pool, conversation_id="race-thread", source_id="runner-race", entries=initial),
        apply_batch(pool, conversation_id="race-thread", source_id="runner-race", entries=initial),
    )
    assert sorted(result.row_writes for result in parallel) == [0, 1]
    assert sum(result.duplicate_events for result in parallel) == 2
    before = await pool.fetchrow(
        "SELECT revision,text_bytes FROM sync_view_row WHERE conversation_id='race-thread' AND row_key='item:race-item'"
    )
    assert before is not None
    manifests_before = await pool.fetchval(
        "SELECT count(*) FROM projected_payload_manifest WHERE conversation_id='race-thread'"
    )
    chunks_before = await pool.fetchval(
        "SELECT count(*) FROM projected_payload_chunk WHERE conversation_id='race-thread'"
    )
    failed_batch = [
        _text_entry("runner-race", 3, "race-item", "+will-rollback"),
        _text_entry("runner-race", 4, "missing-item", "invalid"),
    ]
    with pytest.raises(UnknownItemError):
        await apply_batch(pool, conversation_id="race-thread", source_id="runner-race", entries=failed_batch)
    after_failure = await pool.fetchrow(
        "SELECT through_cursor FROM projection_checkpoint WHERE conversation_id='race-thread'"
    )
    after_failure_row = await pool.fetchrow(
        "SELECT revision,text_bytes FROM sync_view_row WHERE conversation_id='race-thread' AND row_key='item:race-item'"
    )
    after_failure_manifests = await pool.fetchval(
        "SELECT count(*) FROM projected_payload_manifest WHERE conversation_id='race-thread'"
    )
    after_failure_chunks = await pool.fetchval(
        "SELECT count(*) FROM projected_payload_chunk WHERE conversation_id='race-thread'"
    )
    assert after_failure is not None
    assert after_failure_row is not None
    assert int(after_failure["through_cursor"]) == 2
    assert dict(after_failure_row) == dict(before)
    assert after_failure_manifests == manifests_before
    assert after_failure_chunks == chunks_before
    retried = await apply_batch(pool, conversation_id="race-thread", source_id="runner-race", entries=[failed_batch[0]])
    assert retried.through_cursor == 3
    assert retried.row_writes == 1
    assert retried.payload_parts == 1
    return {
        "parallelResults": [result.__dict__ for result in parallel],
        "failedCheckpoint": int(after_failure["through_cursor"]),
        "manifestsBeforeFailure": manifests_before,
        "manifestsAfterFailure": after_failure_manifests,
        "chunksBeforeFailure": chunks_before,
        "chunksAfterFailure": after_failure_chunks,
        "retry": retried.__dict__,
    }


@asynccontextmanager
async def _serve(app: Any) -> AsyncIterator[str]:
    sock = bind_free_port()
    host, port = sock.getsockname()
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    deadline = asyncio.get_running_loop().time() + 15
    try:
        while not server.started:
            if task.done():
                await task
                raise RuntimeError("uvicorn exited before serving")
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("uvicorn did not report startup")
            await asyncio.sleep(0.01)
        yield f"http://{host}:{port}"
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=10)
        except TimeoutError:
            # Electric keeps long-poll requests open while the test tears down.
            # Cancel only this test server after the graceful drain window so
            # shutdown does not hide the original assertion or browser error.
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _connect_postgres_when_ready(dsn: str) -> asyncpg.Pool:
    last_error: BaseException | None = None
    for _ in range(120):
        try:
            connection = await asyncpg.connect(dsn, timeout=2)
            await connection.close()
            return await asyncpg.create_pool(dsn, min_size=1, max_size=8)
        except (asyncpg.PostgresError, OSError, TimeoutError) as error:
            last_error = error
            await asyncio.sleep(0.1)
    raise TimeoutError("PostgreSQL never accepted a connection") from last_error


async def _wait_electric(url: str) -> None:
    async with httpx.AsyncClient(timeout=2) as client:
        for _ in range(180):
            try:
                response = await client.get(f"{url}/v1/health")
                if response.status_code == 200 and response.json().get("status") == "active":
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.1)
    raise TimeoutError("Electric 1.8.0 never reached /v1/health active")


async def _api_get(app_url: str, path: str, *, headers: dict[str, str]) -> httpx.Response:
    async with httpx.AsyncClient(base_url=app_url, timeout=10) as client:
        return await client.get(path, headers=headers)


def _capture_electric(
    page: Page,
    page_name: str,
    records: list[dict[str, Any]],
    tasks: set[asyncio.Task[Any]],
    network_events: list[dict[str, Any]],
    journal_path: Path,
    page_errors: list[str],
) -> None:
    def write_journal() -> None:
        _write_browser_journal(journal_path, network_events, records, page_errors)

    async def capture(response: Response) -> None:
        request = response.request
        is_payload_manifest = "/api/payloads/" in response.url
        is_electric = "/api/electric/" in response.url
        if not is_payload_manifest and not is_electric:
            return
        try:
            body = await response.body()
        except Exception as error:
            records.append({"page": page_name, "url": response.url, "error": str(error)})
            write_journal()
            return
        request_parts = urlsplit(request.url)
        if is_payload_manifest:
            manifest = _parse_body(body)
            records.append(
                {
                    "page": page_name,
                    "kind": "payload-manifest",
                    "method": request.method,
                    "path": request_parts.path,
                    "status": response.status,
                    "responseBytes": len(body),
                    "payloadRef": manifest.get("payloadRef"),
                    "itemId": manifest.get("itemId"),
                    "field": manifest.get("fieldName"),
                    "revision": manifest.get("revision"),
                    "generationId": manifest.get("generationId"),
                    "chunkCount": manifest.get("chunkCount"),
                    "contentBytes": manifest.get("contentBytes"),
                }
            )
            write_journal()
            return
        query = parse_qs(request_parts.query)
        messages = _messages(body)
        row_messages = [message for message in messages if message.get("headers", {}).get("operation")]
        post_body = _parse_body(request.post_data.encode()) if request.post_data else {}
        row_values = [message.get("value", {}) for message in row_messages]
        path_parts = request_parts.path.split("/")
        payload_shape = "payload" in path_parts
        record = {
            "page": page_name,
            "method": request.method,
            "path": request_parts.path,
            "query": {key: values[-1] for key, values in query.items()},
            "subset": {key: post_body[key] for key in ("where", "limit", "offset", "order_by") if key in post_body},
            "status": response.status,
            "responseBytes": len(body),
            "rowCount": len(row_messages),
            "operations": [message["headers"]["operation"] for message in row_messages],
            "controls": [
                message["headers"]["control"] for message in messages if message.get("headers", {}).get("control")
            ],
            "rowKeys": [value.get("row_key") for value in row_values if "row_key" in value][:40],
            "payloadPart": path_parts[-1] if payload_shape else None,
            "payloadRef": path_parts[-2] if payload_shape and len(path_parts) >= 2 else None,
            "chunkIndexes": [value["chunk_index"] for value in row_values if "chunk_index" in value],
            "generationIds": sorted({value["generation_id"] for value in row_values if "generation_id" in value}),
            "chunkRows": [
                {
                    "generationId": value["generation_id"],
                    "chunkIndex": value["chunk_index"],
                    "sourceId": value["source_id"],
                    "contentBytes": value["content_bytes"],
                    "contentSha256": hashlib.sha256(value["content"].encode("utf-8")).hexdigest(),
                }
                for value in row_values
                if "chunk_index" in value
            ],
            "fields": sorted(set().union(*(set(value) for value in row_values))) if row_values else [],
            "containsSensitivePayload": any(secret.encode() in body for secret in SENSITIVE),
            "bodySha256": hashlib.sha256(body).hexdigest(),
            "electricHandle": response.headers.get("electric-handle"),
            "electricOffset": response.headers.get("electric-offset"),
            "proxyInstance": response.headers.get("x-proxy-instance"),
            "gatewayDispatch": response.headers.get("x-gateway-dispatch"),
        }
        records.append(record)
        write_journal()

    def on_response(response: Response) -> None:
        if "/api/electric/" in response.url or "/api/payloads/" in response.url:
            network_events.append(
                {
                    "page": page_name,
                    "event": "response",
                    "method": response.request.method,
                    "url": response.url,
                    "status": response.status,
                    "headers": response.headers,
                    "postData": response.request.post_data,
                }
            )
            write_journal()
        task = asyncio.create_task(capture(response))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def on_request(request: Request) -> None:
        if "/api/electric/" in request.url or "/api/payloads/" in request.url:
            network_events.append(
                {
                    "page": page_name,
                    "event": "request",
                    "method": request.method,
                    "url": request.url,
                    "postData": request.post_data,
                }
            )
            write_journal()

    def on_request_failed(request: Request) -> None:
        if "/api/electric/" in request.url or "/api/payloads/" in request.url:
            network_events.append(
                {
                    "page": page_name,
                    "event": "requestfailed",
                    "method": request.method,
                    "url": request.url,
                    "failure": request.failure,
                }
            )
            write_journal()

    page.on("request", on_request)
    page.on("requestfailed", on_request_failed)
    page.on("response", on_response)


async def _wait_ready(page: Page, *, exact_bigint: bool = False) -> None:
    await page.wait_for_function(
        """() => document.querySelector('[data-testid=sync-app]')?.getAttribute('data-status') === 'ready'
          && document.querySelector('[data-testid=tail-count]')?.textContent?.trim() === '30'""",
        timeout=60_000,
    )
    if exact_bigint:
        await page.wait_for_function(
            "document.querySelector('[data-testid=exact-bigint]')?.textContent?.trim() === 'exact'", timeout=30_000
        )


async def _assert_rendered_payload(
    page: Page, *, expected: str, payload_ref: str, revision: int
) -> dict[str, Any]:
    await page.wait_for_function(
        """(selection) => {
          const body = document.querySelector('[data-testid=payload-body]')
          return body?.dataset.state === 'ready'
            && body.dataset.payloadRef === selection.payloadRef
            && body.dataset.revision === selection.revision
            && body.textContent === selection.content
        }""",
        arg={"content": expected, "payloadRef": payload_ref, "revision": str(revision)},
        timeout=60_000,
    )
    state = await page.get_by_test_id("payload-body").evaluate(
        "body => ({state: body.dataset.state, payloadRef: body.dataset.payloadRef, revision: body.dataset.revision, "
        "latestRef: body.dataset.latestRef, shapeRef: body.dataset.shapeRef, chunkCount: Number(body.dataset.chunkCount), "
        "contentBytes: body.dataset.contentBytes, actualContent: body.textContent ?? ''})"
    )
    actual = state.pop("actualContent")
    expected_hash = hashlib.sha256(expected.encode("utf-8")).hexdigest()
    actual_hash = hashlib.sha256(actual.encode("utf-8")).hexdigest()
    assert actual_hash == expected_hash, {"state": state, "actualHash": actual_hash, "expectedHash": expected_hash}
    state["contentSha256"] = actual_hash
    state["actualMatchesSelectedRevision"] = True
    return cast(dict[str, Any], state)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True, default=str) + "\n")


def _write_browser_journal(
    path: Path, network_events: list[dict[str, Any]], records: list[dict[str, Any]], page_errors: list[str]
) -> None:
    _write_json(path, {"events": network_events, "records": records, "pageErrors": page_errors})


async def test_electric_end_to_end() -> None:
    outputs = undeclared_outputs_dir() / "electric-derisk"
    outputs.mkdir(parents=True, exist_ok=True)
    for image in (ryuk.IMAGE, postgres_18.IMAGE, electric_1_8.IMAGE):
        load_oci_image(image)

    diagnostics: dict[str, Any] = {"versions": {"postgres": "18", "electric": "1.8.0"}}
    response_records: list[dict[str, Any]] = []
    response_tasks: set[asyncio.Task[Any]] = set()
    network_events: list[dict[str, Any]] = []
    page_errors: list[str] = []
    base_cursor = 0

    with Network() as network:
        postgres = (
            LoggedContainer(postgres_18.IMAGE.tag, test_name="electric-derisk-postgres")
            .with_network(network)
            .with_network_aliases("postgres")
            .with_exposed_ports(5432)
            .with_env("POSTGRES_USER", "postgres")
            .with_env("POSTGRES_PASSWORD", "postgres")
            .with_command("postgres -c wal_level=logical -c max_wal_senders=10 -c max_replication_slots=10")
        )
        with postgres:
            pg_host = postgres.get_container_host_ip()
            pg_port = postgres.get_exposed_port(5432)
            dsn = f"postgresql://postgres:postgres@{pg_host}:{pg_port}/postgres"
            pool = await _connect_postgres_when_ready(dsn)
            try:
                await initialize_database(pool)
                await _seed_conversation(pool, "alpha-small", count=SMALL_COUNT, first_anchor=HIGH_CURSOR - SMALL_COUNT)
                await _seed_conversation(pool, "alpha-large", count=LARGE_COUNT, first_anchor=HIGH_CURSOR)
                payload_probes = await _seed_payload_probes(pool)
                base_cursor = await _seed_large_live_rows(pool)
                projector_evidence = await _check_projector_retry_and_race(pool)
                diagnostics["projector"] = projector_evidence
                diagnostics["seededRows"] = {
                    "alpha-small": await pool.fetchval(
                        "SELECT count(*) FROM sync_view_row WHERE conversation_id='alpha-small'"
                    ),
                    "alpha-large": await pool.fetchval(
                        "SELECT count(*) FROM sync_view_row WHERE conversation_id='alpha-large'"
                    ),
                }
                await pool.execute("ANALYZE sync_view_row")
                plans = {
                    "smallTail": await _query_plan(pool, "alpha-small"),
                    "largeTail": await _query_plan(pool, "alpha-large"),
                    "largeExclusiveBefore": await _query_plan(pool, "alpha-large", HIGH_CURSOR + LARGE_COUNT - 24),
                }
                _write_json(outputs / "query-plans.json", plans)
                nodes = _flatten_plan(plans["largeExclusiveBefore"][0]["Plan"])
                assert any(
                    node.get("Index Name") == "sync_view_row_tail" and node.get("Actual Rows", 10**9) <= 30
                    for node in nodes
                ), nodes
                await pool.execute("ANALYZE projected_payload_chunk")
                payload_plans = {
                    name: await _payload_chunk_plan(pool, probe) for name, probe in payload_probes.items()
                }
                _write_json(outputs / "payload-query-plans.json", payload_plans)
                for name, plan in payload_plans.items():
                    nodes = _flatten_plan(plan[0]["Plan"])
                    selected_count = payload_probes[name]["chunkCount"]
                    assert any(
                        node.get("Index Name") == "projected_payload_chunk_pkey"
                        and node.get("Actual Rows") == selected_count
                        for node in nodes
                    ), {"name": name, "plan": nodes, "history": payload_probes[name]}
                diagnostics["payloadReconstruction"] = {
                    "history": payload_probes,
                    "plans": payload_plans,
                }

                electric = (
                    LoggedContainer(electric_1_8.IMAGE.tag, test_name="electric-derisk-electric-first")
                    .with_network(network)
                    .with_network_aliases("electric")
                    .with_exposed_ports(3000)
                    .with_env("DATABASE_URL", "postgresql://postgres:postgres@postgres:5432/postgres?sslmode=disable")
                    .with_env("ELECTRIC_INSECURE", "true")
                )
                with electric:
                    electric_url = f"http://{electric.get_container_host_ip()}:{electric.get_exposed_port(3000)}"
                    await _wait_electric(electric_url)
                    subset_gate = SubsetGate()
                    electric_shape_url = f"{electric_url}/v1/shape"
                    proxy_one = create_electric_proxy(electric_shape_url, "proxy-1", pool)
                    proxy_two = create_electric_proxy(electric_shape_url, "proxy-2", pool)
                    proxy_one.state.subset_gate = subset_gate
                    proxy_two.state.subset_gate = subset_gate
                    async with (
                        _serve(proxy_one) as proxy_one_url,
                        _serve(proxy_two) as proxy_two_url,
                        _serve(
                            create_gateway(
                                pool,
                                [proxy_one_url, proxy_two_url],
                                get_required_path("_main/x/agentplane_sync/frontend/dist"),
                            )
                        ) as app_url,
                    ):
                        async with httpx.AsyncClient(base_url=app_url, timeout=10) as api:
                            exact_page = await api.get(
                                "/api/history/alpha-small",
                                params={"before": str(HIGH_CURSOR + 1), "limit": 1},
                                headers=AUTH,
                            )
                            assert exact_page.status_code == 200, exact_page.text
                            exact_row = exact_page.json()["rows"][0]
                            assert exact_row["anchor"] == str(HIGH_CURSOR)
                            assert exact_row["row_key"] == f"item:alpha-small-seed-{SMALL_COUNT}"
                            exclusive = await api.get(
                                "/api/history/alpha-small",
                                params={"before": str(HIGH_CURSOR), "limit": 1},
                                headers=AUTH,
                            )
                            assert int(exclusive.json()["rows"][0]["anchor"]) < HIGH_CURSOR
                            assert (await api.get("/api/history/alpha-large", params={"limit": 30})).status_code == 401
                            denied = await api.get("/api/electric/not-authorized?offset=now", headers=AUTH)
                            assert denied.status_code == 403, denied.text
                            get_override = await api.get(
                                "/api/electric/alpha-large?offset=now&table=pg_class", headers=AUTH
                            )
                            assert get_override.status_code == 400, get_override.text
                            post_override = await api.post(
                                "/api/electric/alpha-large?offset=now",
                                headers=AUTH,
                                json={"table": "pg_class", "where": "anchor < 7"},
                            )
                            assert post_override.status_code == 400, post_override.text
                            where_override = await api.post(
                                "/api/electric/alpha-large?offset=now",
                                headers=AUTH,
                                json={"where": "anchor < 7 AND conversation_id = 'alpha-small'", "limit": 30},
                            )
                            assert where_override.status_code == 400, where_override.text
                            empty_arguments_ref = await pool.fetchval(
                                "SELECT arguments_payload_ref FROM sync_view_row WHERE conversation_id='alpha-large' AND row_key='item:tool-row'"
                            )
                            assert isinstance(empty_arguments_ref, str)
                            exact_empty = await api.get(
                                f"/api/payloads/alpha-large/{empty_arguments_ref}", headers=AUTH
                            )
                            assert exact_empty.status_code == 200, exact_empty.text
                            assert exact_empty.json()["payloadRef"] == empty_arguments_ref
                            assert exact_empty.json()["present"] is True
                            assert exact_empty.json()["chunkCount"] == 0
                            assert exact_empty.json()["contentBytes"] == "0"
                            unknown_manifest = await api.get(
                                "/api/payloads/alpha-large/pr1_expired-revision", headers=AUTH
                            )
                            assert unknown_manifest.status_code == 410, unknown_manifest.text
                            payload_get_override = await api.get(
                                f"/api/electric/alpha-large/payload/{empty_arguments_ref}/chunks?offset=now&table=pg_class",
                                headers=AUTH,
                            )
                            assert payload_get_override.status_code == 400, payload_get_override.text
                            payload_post_override = await api.post(
                                f"/api/electric/alpha-large/payload/{empty_arguments_ref}/chunks?offset=now",
                                headers=AUTH,
                                json={"table": "pg_class", "where": "chunk_index >= 0"},
                            )
                            assert payload_post_override.status_code == 400, payload_post_override.text
                            payload_limit_override = await api.post(
                                f"/api/electric/alpha-large/payload/{empty_arguments_ref}/chunks?offset=now",
                                headers=AUTH,
                                json={"limit": 1},
                            )
                            assert payload_limit_override.status_code == 400, payload_limit_override.text
                            payload_log_override = await api.get(
                                f"/api/electric/alpha-large/payload/{empty_arguments_ref}/chunks?offset=now&log=changes_only",
                                headers=AUTH,
                            )
                            assert payload_log_override.status_code == 400, payload_log_override.text
                            foreign_payload_ref = payload_probes["payload-small"]["payloadRef"]
                            wrong_conversation_ref = await api.get(
                                f"/api/electric/alpha-large/payload/{foreign_payload_ref}/chunks?offset=now",
                                headers=AUTH,
                            )
                            assert wrong_conversation_ref.status_code == 410, wrong_conversation_ref.text
                        diagnostics["auth"] = {
                            "crossConversation": denied.status_code,
                            "getTableOverride": get_override.status_code,
                            "postTableOverride": post_override.status_code,
                            "whereConversationOverride": where_override.status_code,
                            "payloadExactEmpty": exact_empty.json(),
                            "payloadUnknownRevision": unknown_manifest.status_code,
                            "payloadTableOverride": payload_get_override.status_code,
                            "payloadPostOverride": payload_post_override.status_code,
                            "payloadLimitOverride": payload_limit_override.status_code,
                            "payloadLogOverride": payload_log_override.status_code,
                            "payloadForeignConversationRef": wrong_conversation_ref.status_code,
                        }

                        route_control: dict[str, Any] = {
                            "badHandle": False,
                            "badHandleRequest": None,
                            "disconnectNextPage1Live": False,
                            "disconnectRequest": None,
                            "disconnectFailure": None,
                            "recordResetSubsets": False,
                            "holdManifestRef": None,
                            "manifestHeld": asyncio.Event(),
                            "releaseManifest": asyncio.Event(),
                            "manifestRouteFinished": asyncio.Event(),
                            "manifestRouteError": None,
                        }
                        disconnect_failed = asyncio.Event()
                        recovery_responses: asyncio.Queue[Response] = asyncio.Queue()
                        reset_subset_responses: asyncio.Queue[Response] = asyncio.Queue()
                        page_references: dict[str, Page] = {}
                        async with async_playwright() as playwright:
                            browser = await playwright.chromium.launch(
                                headless=True, executable_path=chromium_executable(), args=CONTAINER_BASE_BROWSER_ARGS
                            )
                            context: BrowserContext = await browser.new_context(
                                viewport={"width": 1280, "height": 900},
                                record_har_path=str(outputs / "browser.har"),
                                record_har_content="embed",
                                record_har_mode="full",
                            )
                            await context.add_init_script(
                                "window.localStorage.setItem('spike-token', 'derisk-test-token')"
                            )
                            await context.tracing.start(screenshots=True, snapshots=True, sources=True)

                            async def route_request(route: Any, request: Request) -> None:
                                parts = urlsplit(request.url)
                                query = parse_qs(parts.query)
                                if (
                                    route_control["disconnectNextPage1Live"]
                                    and query.get("live") == ["true"]
                                    and page_references.get("page-1") is request.frame.page
                                ):
                                    route_control["disconnectNextPage1Live"] = False
                                    route_control["disconnectRequest"] = {
                                        "url": request.url,
                                        "method": request.method,
                                        "page": "page-1",
                                    }
                                    # Fail a real live poll at the browser boundary, then
                                    # keep that client offline while the database advances.
                                    await context.set_offline(True)
                                    await route.abort("internetdisconnected")
                                    return
                                if (
                                    route_control["badHandleRequest"] is None
                                    and route_control["badHandle"]
                                    and query.get("handle")
                                    and query.get("offset", [""])[-1] not in {"now", "-1"}
                                    and page_references.get("page-1") is request.frame.page
                                ):
                                    changed = query.copy()
                                    changed["handle"] = ["expired-derisk-handle"]
                                    new_query = "&".join(
                                        f"{key}={value}" for key, values in changed.items() for value in values
                                    )
                                    url = urlunsplit(
                                        (parts.scheme, parts.netloc, parts.path, new_query, parts.fragment)
                                    )
                                    route_control["badHandle"] = False
                                    route_control["badHandleRequest"] = {"url": request.url, "method": request.method}
                                    await route.continue_(url=url)
                                    return
                                await route.continue_()

                            async def route_payload_manifest(route: Any, request: Request) -> None:
                                target_ref = route_control["holdManifestRef"]
                                if target_ref is not None and request.url.endswith(str(target_ref)):
                                    route_control["manifestHeld"].set()
                                    await route_control["releaseManifest"].wait()
                                    try:
                                        await route.continue_()
                                    except Exception as error:
                                        route_control["manifestRouteError"] = str(error)
                                    finally:
                                        route_control["manifestRouteFinished"].set()
                                    return
                                await route.continue_()

                            await context.route("**/api/electric/**", route_request)
                            await context.route("**/api/payloads/**", route_payload_manifest)
                            journal_path = outputs / "browser-network-journal.json"

                            def record_page_error(error: Any) -> None:
                                page_errors.append(str(error))
                                _write_browser_journal(journal_path, network_events, response_records, page_errors)

                            def record_console_error(message: Any) -> None:
                                if message.type == "error":
                                    network_events.append(
                                        {"event": "console", "type": message.type, "text": message.text}
                                    )
                                    _write_browser_journal(journal_path, network_events, response_records, page_errors)

                            page = await context.new_page()
                            page_references["page-1"] = page

                            def record_disconnect_failure(request: Request) -> None:
                                disconnect_request = route_control["disconnectRequest"]
                                if disconnect_request is not None and request.url == disconnect_request["url"]:
                                    route_control["disconnectFailure"] = request.failure
                                    disconnect_failed.set()

                            _capture_electric(
                                page,
                                "page-1",
                                response_records,
                                response_tasks,
                                network_events,
                                journal_path,
                                page_errors,
                            )

                            def capture_recovery_response(response: Response) -> None:
                                query = parse_qs(urlsplit(response.url).query)
                                if (
                                    "/api/electric/alpha-large" in response.url
                                    and "cache-buster" in query
                                    and query.get("offset") == ["-1"]
                                ):
                                    recovery_responses.put_nowait(response)

                            def capture_reset_subset_response(response: Response) -> None:
                                parts = urlsplit(response.url)
                                query = parse_qs(parts.query)
                                if (
                                    route_control["recordResetSubsets"]
                                    and parts.path == "/api/electric/alpha-large"
                                    and "subset__limit" in query
                                ):
                                    reset_subset_responses.put_nowait(response)

                            page.on("response", capture_recovery_response)
                            page.on("response", capture_reset_subset_response)
                            page.on("pageerror", record_page_error)
                            page.on("console", record_console_error)
                            page.on("requestfailed", record_disconnect_failure)
                            await page.goto(f"{app_url}/?conversation=alpha-small", wait_until="domcontentloaded")
                            try:
                                await _wait_ready(page, exact_bigint=True)
                            except Exception:
                                html = await page.content()
                                await page.screenshot(path=outputs / "bootstrap-failed.png", full_page=True)
                                await context.close()
                                await asyncio.gather(*tuple(response_tasks), return_exceptions=True)
                                _write_json(
                                    outputs / "bootstrap-debug.json",
                                    {
                                        "events": network_events,
                                        "pageErrors": page_errors,
                                        "html": html,
                                        "url": page.url,
                                    },
                                )
                                raise
                            await page.screenshot(path=outputs / "alpha-small-tail.png", full_page=True)
                            await asyncio.gather(*tuple(response_tasks), return_exceptions=True)
                            small_records = [
                                record
                                for record in response_records
                                if record.get("path") == "/api/electric/alpha-small"
                            ]
                            small_rows = sum(record.get("rowCount", 0) for record in small_records)
                            small_bytes = sum(record.get("responseBytes", 0) for record in small_records)
                            assert small_rows <= 30, small_records
                            assert all(not record.get("containsSensitivePayload") for record in small_records)

                            await page.goto(f"{app_url}/?conversation=alpha-large", wait_until="domcontentloaded")
                            await _wait_ready(page)
                            initial_tail = await page.locator("[data-testid=row]").evaluate_all(
                                "elements => elements.map(element => ({key: element.dataset.rowKey, anchor: element.dataset.anchor, kind: element.dataset.kind}))"
                            )
                            initial_keys = {row["key"] for row in initial_tail}
                            large_records = [
                                record
                                for record in response_records
                                if record.get("path") == "/api/electric/alpha-large"
                            ]
                            large_rows = sum(record.get("rowCount", 0) for record in large_records)
                            large_bytes = sum(record.get("responseBytes", 0) for record in large_records)
                            assert len(initial_tail) == 30
                            assert large_rows <= 30, large_records
                            assert large_bytes <= max(small_bytes * 2, 4096), {
                                "small": small_bytes,
                                "large": large_bytes,
                            }
                            assert all(not record.get("containsSensitivePayload") for record in large_records)
                            assert all(
                                not (
                                    set(record.get("fields", []))
                                    & {"text", "arguments", "output", "reasoning", "content"}
                                )
                                for record in large_records
                            ), large_records
                            await page.screenshot(path=outputs / "alpha-large-tail.png", full_page=True)

                            min_history_anchor = min(
                                int(row["anchor"]) for row in initial_tail if row["kind"] == "item"
                            )
                            subset_gate.active = True
                            await page.get_by_role("button", name="Load older").click()
                            await asyncio.wait_for(subset_gate.arrived.wait(), timeout=30)
                            snapshot_messages = _messages(subset_gate.response_body)
                            snapshot_rows = [
                                message.get("value", {})
                                for message in snapshot_messages
                                if message.get("headers", {}).get("operation") == "insert"
                            ]
                            target_item_id = f"alpha-large-seed-{LARGE_COUNT - 25}"
                            target_key = f"item:{target_item_id}"
                            stale_snapshot = next(row for row in snapshot_rows if row.get("row_key") == target_key)
                            stale_revision = int(stale_snapshot["revision"])
                            assert stale_revision < base_cursor
                            gate_update = [
                                _text_entry(SOURCE, base_cursor + 1, "live-item", " +during-history"),
                                _entry(
                                    SOURCE,
                                    base_cursor + 2,
                                    "item_completed",
                                    event_pb2.ItemCompleted(item_id=target_item_id, text="late completion"),
                                ),
                            ]
                            gate_result = await apply_batch(
                                pool, conversation_id="alpha-large", source_id=SOURCE, entries=gate_update
                            )
                            assert gate_result.row_writes == 2
                            assert gate_result.payload_parts == 2
                            subset_gate.release.set()
                            await page.wait_for_function(
                                "document.querySelector('[data-testid=history-count]')?.textContent?.trim() === '30'",
                                timeout=45_000,
                            )
                            await page.wait_for_function(
                                """(expected) => {
                                  const row = [...document.querySelectorAll('[data-row-key]')]
                                    .find(node => node.dataset.rowKey === expected.key)
                                  if (!row) return false
                                  return row.dataset.revision === expected.revision && row.dataset.status === 'complete'
                                }""",
                                arg={"key": target_key, "revision": str(base_cursor + 2)},
                                timeout=45_000,
                            )
                            history_rows = await page.locator("[data-testid=row]").evaluate_all(
                                "elements => elements.map(element => ({key: element.dataset.rowKey, anchor: element.dataset.anchor}))"
                            )
                            new_history = [row for row in history_rows if row["key"] not in initial_keys]
                            assert len(new_history) == 30
                            assert max(int(row["anchor"]) for row in new_history) < min_history_anchor
                            viewport = page.get_by_test_id("viewport")
                            await viewport.evaluate("element => { element.scrollTop = 160 }")
                            await page.get_by_role("button", name="Load older").click()
                            await page.wait_for_function("window.__syncEvidence.scroll.length > 0", timeout=45_000)
                            scroll_evidence = await page.evaluate("window.__syncEvidence.scroll.at(-1)")
                            _write_json(outputs / "scroll-evidence.json", scroll_evidence)
                            await page.screenshot(path=outputs / "alpha-large-scroll-anchor.png", full_page=True)
                            assert abs(scroll_evidence["correctionPx"]) > 1, scroll_evidence
                            assert abs(scroll_evidence["delta"]) <= 1, scroll_evidence
                            await page.wait_for_function(
                                "document.querySelector('[data-testid=history-count]')?.textContent?.trim() === '60'",
                                timeout=45_000,
                            )
                            await page.screenshot(path=outputs / "alpha-large-history.png", full_page=True)

                            page_two = await context.new_page()
                            _capture_electric(
                                page_two,
                                "page-2",
                                response_records,
                                response_tasks,
                                network_events,
                                journal_path,
                                page_errors,
                            )
                            page_two.on("pageerror", record_page_error)
                            page_two.on("console", record_console_error)
                            await page_two.goto(f"{app_url}/?conversation=alpha-large", wait_until="domcontentloaded")
                            await _wait_ready(page_two)
                            await page_two.wait_for_function(
                                f"document.querySelector('[data-testid=atomic-state]')?.textContent?.trim() === '{base_cursor - 3}|model-v1|pending'",
                                timeout=30_000,
                            )

                            payload_ref_r = await pool.fetchval(
                                "SELECT text_payload_ref FROM sync_view_row WHERE conversation_id='alpha-large' AND row_key='item:live-item'"
                            )
                            assert isinstance(payload_ref_r, str)
                            payload_requests_before_selection = (await _api_get(
                                app_url, "/api/proxy-metrics", headers=AUTH
                            )).json()["payloadRequests"]
                            payload_gate = PayloadGate(payload_ref_r, "chunks")
                            proxy_one.state.payload_gate = payload_gate
                            proxy_two.state.payload_gate = payload_gate
                            await page.get_by_role("button", name="Open text").click()
                            await asyncio.wait_for(payload_gate.arrived.wait(), timeout=45)
                            r_snapshot_messages = _messages(payload_gate.response_body)
                            r_snapshot_rows = [
                                message["value"]
                                for message in r_snapshot_messages
                                if message.get("headers", {}).get("operation")
                            ]
                            r_snapshot_rows.sort(key=lambda row: int(row["chunk_index"]))
                            expected_r_content = "seed text +during-history"
                            assert [int(row["chunk_index"]) for row in r_snapshot_rows] == [0, 1], r_snapshot_rows
                            assert "".join(row["content"] for row in r_snapshot_rows) == expected_r_content
                            r_snapshot_evidence = {
                                "payloadRef": payload_ref_r,
                                "revision": str(base_cursor + 1),
                                "chunkIndexes": [int(row["chunk_index"]) for row in r_snapshot_rows],
                                "contentBytes": sum(int(row["content_bytes"]) for row in r_snapshot_rows),
                                "contentSha256": hashlib.sha256(expected_r_content.encode("utf-8")).hexdigest(),
                                "electricResponseBytes": len(payload_gate.response_body),
                            }

                            r_plus_one = base_cursor + 3
                            first_result = await apply_batch(
                                pool,
                                conversation_id="alpha-large",
                                source_id=SOURCE,
                                entries=[_text_entry(SOURCE, r_plus_one, "live-item", " +first")],
                            )
                            assert first_result == ApplyResult(r_plus_one, 0, 1, 1), first_result
                            payload_ref_r_plus_one = await pool.fetchval(
                                "SELECT text_payload_ref FROM sync_view_row WHERE conversation_id='alpha-large' AND row_key='item:live-item'"
                            )
                            assert isinstance(payload_ref_r_plus_one, str)
                            assert payload_ref_r_plus_one != payload_ref_r
                            await page.wait_for_function(
                                """(refs) => {
                                  const row = document.querySelector('[data-row-key="item:live-item"]')
                                  const body = document.querySelector('[data-testid=payload-body]')
                                  return row?.dataset.textPayloadRef === refs.latest
                                    && body?.dataset.payloadRef === refs.selected
                                    && body?.dataset.latestRef === refs.latest
                                    && body?.dataset.state === 'hydrating'
                                    && body.textContent === ''
                                }""",
                                arg={"selected": payload_ref_r, "latest": payload_ref_r_plus_one},
                                timeout=30_000,
                            )
                            r_while_r_plus_one_arrived = await page.get_by_test_id("payload-body").evaluate(
                                "body => ({state: body.dataset.state, payloadRef: body.dataset.payloadRef, "
                                "latestRef: body.dataset.latestRef, revision: body.dataset.revision, content: body.textContent ?? ''})"
                            )
                            assert r_while_r_plus_one_arrived == {
                                "state": "hydrating",
                                "payloadRef": payload_ref_r,
                                "latestRef": payload_ref_r_plus_one,
                                "revision": str(base_cursor + 1),
                                "content": "",
                            }, r_while_r_plus_one_arrived
                            payload_gate.release.set()
                            await page.wait_for_function(
                                """(expected) => window.__syncEvidence.payloadStates.some(state =>
                                  state.payloadRef === expected.payloadRef
                                  && state.revision === expected.revision
                                  && state.contentSha256 === expected.contentSha256)""",
                                arg={
                                    "payloadRef": payload_ref_r,
                                    "revision": str(base_cursor + 1),
                                    "contentSha256": hashlib.sha256(expected_r_content.encode("utf-8")).hexdigest(),
                                },
                                timeout=45_000,
                            )
                            r_state = await page.evaluate(
                                """(ref) => window.__syncEvidence.payloadStates.find(state => state.payloadRef === ref)""",
                                arg=payload_ref_r,
                            )
                            assert r_state["latestRef"] == payload_ref_r_plus_one, r_state
                            r_plus_one_content = expected_r_content + " +first"
                            r_plus_one_visible = await _assert_rendered_payload(
                                page,
                                expected=r_plus_one_content,
                                payload_ref=payload_ref_r_plus_one,
                                revision=r_plus_one,
                            )
                            _write_json(
                                outputs / "payload-race-evidence.json",
                                {
                                    "selectedRWhileRPlusOneWasCurrent": r_state,
                                    "hydrationGateSnapshot": r_snapshot_evidence,
                                    "heldUiBeforeRelease": r_while_r_plus_one_arrived,
                                    "renderedRPlusOne": r_plus_one_visible,
                                },
                            )
                            payload_requests = (await _api_get(app_url, "/api/proxy-metrics", headers=AUTH)).json()[
                                "payloadRequests"
                            ]
                            selected_text_requests = payload_requests[len(payload_requests_before_selection) :]
                            assert {request["field"] for request in selected_text_requests} == {"text"}
                            assert sum(
                                request["part"] == "chunks" and request["itemId"] == "live-item"
                                for request in selected_text_requests
                            ) == 1, selected_text_requests
                            assert any(
                                request["part"] == "manifest"
                                and request["payloadRef"] == payload_ref_r
                                and request["revision"] == str(base_cursor + 1)
                                for request in selected_text_requests
                            ), selected_text_requests

                            main_batch = [
                                _text_entry(SOURCE, base_cursor + 4, "live-item", " +second"),
                                _entry(
                                    SOURCE,
                                    base_cursor + 5,
                                    "tool_arguments_delta",
                                    event_pb2.ToolArgumentsDelta(item_id="tool-row", partial_json='{"x":'),
                                ),
                                _entry(
                                    SOURCE,
                                    base_cursor + 6,
                                    "tool_arguments_delta",
                                    event_pb2.ToolArgumentsDelta(item_id="tool-row", partial_json="1}"),
                                ),
                                _entry(
                                    SOURCE,
                                    base_cursor + 7,
                                    "tool_output_delta",
                                    event_pb2.ToolOutputDelta(item_id="tool-row", text="SENSITIVE_OUTPUT_AFTER"),
                                ),
                                _entry(
                                    SOURCE,
                                    base_cursor + 8,
                                    "model_changed",
                                    event_pb2.ModelChanged(
                                        command_id="sync-command", previous_model="model-v1", model="model-v2"
                                    ),
                                ),
                            ]
                            main_result = await apply_batch(
                                pool, conversation_id="alpha-large", source_id=SOURCE, entries=main_batch
                            )
                            assert main_result == ApplyResult(base_cursor + 8, 0, 4, 4), main_result
                            expected_atomic = f"{base_cursor + 6}|model-v2|applied"
                            for opened in (page, page_two):
                                await opened.wait_for_function(
                                    "(state) => document.querySelector('[data-testid=atomic-state]')?.textContent?.trim() === state",
                                    arg=expected_atomic,
                                    timeout=45_000,
                                )
                            states = await page.evaluate("window.__syncEvidence.atomicStates")
                            allowed_states = {
                                (str(base_cursor - 3), "model-v1", "pending"),
                                (str(base_cursor + 6), "model-v2", "applied"),
                            }
                            assert all(tuple(state) in allowed_states for state in states), states
                            await _assert_rendered_payload(
                                page,
                                expected=r_plus_one_content + " +second",
                                payload_ref=payload_ref_r_plus_one,
                                revision=base_cursor + 4,
                            )
                            await page.get_by_role("button", name="Open arguments").click()
                            arguments_ref = await pool.fetchval(
                                "SELECT arguments_payload_ref FROM sync_view_row WHERE conversation_id='alpha-large' AND row_key='item:tool-row'"
                            )
                            arguments_visible = await _assert_rendered_payload(
                                page, expected='{"x":1}', payload_ref=arguments_ref, revision=base_cursor + 6
                            )
                            independent_output = await apply_batch(
                                pool,
                                conversation_id="alpha-large",
                                source_id=SOURCE,
                                entries=[
                                    _entry(
                                        SOURCE,
                                        base_cursor + 9,
                                        "tool_output_delta",
                                        event_pb2.ToolOutputDelta(
                                            item_id="tool-row", text="SENSITIVE_OUTPUT_INDEPENDENCE"
                                        ),
                                    )
                                ],
                            )
                            assert independent_output == ApplyResult(base_cursor + 9, 0, 1, 1), independent_output
                            output_ref_after_independent = await pool.fetchval(
                                "SELECT output_payload_ref FROM sync_view_row WHERE conversation_id='alpha-large' AND row_key='item:tool-row'"
                            )
                            assert output_ref_after_independent != arguments_ref
                            arguments_after_output = await page.get_by_test_id("payload-body").evaluate(
                                "body => ({state: body.dataset.state, payloadRef: body.dataset.payloadRef, "
                                "latestRef: body.dataset.latestRef, revision: body.dataset.revision, content: body.textContent ?? ''})"
                            )
                            assert arguments_after_output == {
                                "state": "ready",
                                "payloadRef": arguments_ref,
                                "latestRef": arguments_ref,
                                "revision": str(base_cursor + 6),
                                "content": '{"x":1}',
                            }, arguments_after_output
                            await page.get_by_role("button", name="Open output").click()
                            output_before_replacement = await _assert_rendered_payload(
                                page,
                                expected="SENSITIVE_INITIAL_OUTPUTSENSITIVE_OUTPUT_AFTERSENSITIVE_OUTPUT_INDEPENDENCE",
                                payload_ref=output_ref_after_independent,
                                revision=base_cursor + 9,
                            )

                            older_tool_events = [
                                _entry(
                                    SOURCE,
                                    base_cursor + 10,
                                    "tool_output_delta",
                                    event_pb2.ToolOutputDelta(item_id="older-tool-row", text=" +late-tool-bytes"),
                                ),
                                _entry(
                                    SOURCE,
                                    base_cursor + 11,
                                    "item_completed",
                                    event_pb2.ItemCompleted(
                                        item_id="older-tool-row",
                                        tool=event_pb2.ToolResult(output="REPLACED_OLDER_OUTPUT", succeeded=True),
                                    ),
                                ),
                            ]
                            older_tool_result = await apply_batch(
                                pool, conversation_id="alpha-large", source_id=SOURCE, entries=older_tool_events
                            )
                            assert older_tool_result == ApplyResult(base_cursor + 11, 0, 1, 2), older_tool_result
                            await page.wait_for_function(
                                """(revision) => {
                                  const row = document.querySelector('[data-row-key="item:older-tool-row"]')
                                  return row?.dataset.outputRevision === revision
                                }""",
                                arg=str(base_cursor + 11),
                                timeout=45_000,
                            )
                            still_selected_output = await page.get_by_test_id("payload-body").evaluate(
                                "body => ({payloadRef: body.dataset.payloadRef, latestRef: body.dataset.latestRef, "
                                "content: body.textContent ?? ''})"
                            )
                            assert still_selected_output == {
                                "payloadRef": output_ref_after_independent,
                                "latestRef": output_ref_after_independent,
                                "content": "SENSITIVE_INITIAL_OUTPUTSENSITIVE_OUTPUT_AFTERSENSITIVE_OUTPUT_INDEPENDENCE",
                            }, still_selected_output
                            replacement_result = await apply_batch(
                                pool,
                                conversation_id="alpha-large",
                                source_id=SOURCE,
                                entries=[
                                    _entry(
                                        SOURCE,
                                        base_cursor + 12,
                                        "item_completed",
                                        event_pb2.ItemCompleted(
                                            item_id="tool-row",
                                            tool=event_pb2.ToolResult(
                                                output="REPLACED_TOOL_OUTPUT", succeeded=True
                                            ),
                                        ),
                                    )
                                ],
                            )
                            assert replacement_result == ApplyResult(base_cursor + 12, 0, 1, 1), replacement_result
                            tool_replacement_ref = await pool.fetchval(
                                "SELECT output_payload_ref FROM sync_view_row WHERE conversation_id='alpha-large' AND row_key='item:tool-row'"
                            )
                            tool_output_replacement = await _assert_rendered_payload(
                                page,
                                expected="REPLACED_TOOL_OUTPUT",
                                payload_ref=tool_replacement_ref,
                                revision=base_cursor + 12,
                            )
                            tool_shape_ref = tool_output_replacement["shapeRef"]
                            output_requests_before_close = len(
                                [
                                    request
                                    for request in (await _api_get(app_url, "/api/proxy-metrics", headers=AUTH)).json()[
                                        "payloadRequests"
                                    ]
                                    if request["field"] == "output" and request["itemId"] == "tool-row"
                                ]
                            )
                            await page.get_by_role("button", name="Close payload").click()
                            await page.wait_for_function(
                                """(shapeRef) => window.__syncEvidence.retiredPayloadCollections.some(entry =>
                                  entry.shapeRef === shapeRef && entry.sizeAfterCleanup === 0
                                  && entry.subscribersAfterCleanup === 0 && entry.statusAfterCleanup === 'cleaned-up')""",
                                arg=tool_shape_ref,
                                timeout=30_000,
                            )
                            closed_result = await apply_batch(
                                pool,
                                conversation_id="alpha-large",
                                source_id=SOURCE,
                                entries=[
                                    _entry(
                                        SOURCE,
                                        base_cursor + 13,
                                        "item_completed",
                                        event_pb2.ItemCompleted(
                                            item_id="tool-row",
                                            tool=event_pb2.ToolResult(output="", succeeded=True),
                                        ),
                                    )
                                ],
                            )
                            assert closed_result == ApplyResult(base_cursor + 13, 0, 1, 1), closed_result
                            await page.wait_for_function(
                                """(ref) => document.querySelector('[data-row-key="item:tool-row"]')?.dataset.outputPayloadRef === ref""",
                                arg=await pool.fetchval(
                                    "SELECT output_payload_ref FROM sync_view_row WHERE conversation_id='alpha-large' AND row_key='item:tool-row'"
                                ),
                                timeout=45_000,
                            )
                            payload_after_close = (await _api_get(app_url, "/api/proxy-metrics", headers=AUTH)).json()[
                                "payloadRequests"
                            ]
                            assert (
                                len([
                                    request for request in payload_after_close
                                    if request["field"] == "output" and request["itemId"] == "tool-row"
                                ])
                                == output_requests_before_close
                            )
                            assert await page.get_by_test_id("payload-body").inner_text() == ""
                            empty_output_ref = await pool.fetchval(
                                "SELECT output_payload_ref FROM sync_view_row WHERE conversation_id='alpha-large' AND row_key='item:tool-row'"
                            )
                            await page.get_by_role("button", name="Open output").click()
                            empty_output_visible = await _assert_rendered_payload(
                                page, expected="", payload_ref=empty_output_ref, revision=base_cursor + 13
                            )
                            assert empty_output_visible["chunkCount"] == 0, empty_output_visible
                            older_output_ref = await pool.fetchval(
                                "SELECT output_payload_ref FROM sync_view_row WHERE conversation_id='alpha-large' AND row_key='item:older-tool-row'"
                            )
                            route_control["holdManifestRef"] = f"/api/payloads/alpha-large/{older_output_ref}"
                            await page.get_by_role("button", name="Open older output").click()
                            await asyncio.wait_for(route_control["manifestHeld"].wait(), timeout=30)
                            try:
                                await page.get_by_role("button", name="Open arguments").click()
                                await _assert_rendered_payload(
                                    page,
                                    expected='{"x":1}',
                                    payload_ref=arguments_ref,
                                    revision=base_cursor + 6,
                                )
                            finally:
                                route_control["releaseManifest"].set()
                            await asyncio.wait_for(route_control["manifestRouteFinished"].wait(), timeout=30)
                            route_control["holdManifestRef"] = None
                            arguments_after_stale_manifest = await page.get_by_test_id("payload-body").evaluate(
                                "body => ({field: body.dataset.field, state: body.dataset.state, "
                                "payloadRef: body.dataset.payloadRef, revision: body.dataset.revision, content: body.textContent ?? ''})"
                            )
                            assert arguments_after_stale_manifest == {
                                "field": "arguments",
                                "state": "ready",
                                "payloadRef": arguments_ref,
                                "revision": str(base_cursor + 6),
                                "content": '{"x":1}',
                            }, arguments_after_stale_manifest
                            stale_manifest_requests = (await _api_get(
                                app_url, "/api/proxy-metrics", headers=AUTH
                            )).json()["payloadRequests"]
                            assert any(
                                request["part"] == "manifest" and request["payloadRef"] == older_output_ref
                                for request in stale_manifest_requests
                            ), stale_manifest_requests
                            await page.get_by_role("button", name="Open older output").click()
                            older_output_visible = await _assert_rendered_payload(
                                page,
                                expected="REPLACED_OLDER_OUTPUT",
                                payload_ref=older_output_ref,
                                revision=base_cursor + 11,
                            )
                            _write_json(
                                outputs / "exact-revisions-evidence.json",
                                {
                                    "textRWhileRPlusOneCurrent": r_state,
                                    "textRPlusOne": r_plus_one_visible,
                                    "arguments": arguments_visible,
                                    "argumentsAfterOutputUpdate": arguments_after_output,
                                    "outputBeforeReplacement": output_before_replacement,
                                    "toolOutputReplacement": tool_output_replacement,
                                    "emptyToolOutput": empty_output_visible,
                                    "olderToolOutputReplacement": older_output_visible,
                                    "staleManifestSwitch": {
                                        "heldRef": older_output_ref,
                                        "renderedArgumentsAfterLateResponse": arguments_after_stale_manifest,
                                        "routeError": route_control["manifestRouteError"],
                                    },
                                    "retiredOutputShapeRef": tool_shape_ref,
                                    "closedChunkRequestsStayedAt": output_requests_before_close,
                                },
                            )
                            await page.screenshot(path=outputs / "alpha-large-updated.png", full_page=True)

                            head_cursor = base_cursor + 14
                            head_result = await apply_batch(
                                pool,
                                conversation_id="alpha-large",
                                source_id=SOURCE,
                                entries=[
                                    _entry(
                                        SOURCE,
                                        head_cursor,
                                        "item_started",
                                        event_pb2.ItemStarted(
                                            item_id="head-arrival", kind=event_pb2.ITEM_KIND_ASSISTANT_TEXT
                                        ),
                                    )
                                ],
                            )
                            assert head_result.row_writes == 1
                            for opened in (page, page_two):
                                await opened.wait_for_function(
                                    "document.querySelector('[data-testid=tail-count]')?.textContent?.trim() === '30' && [...document.querySelectorAll('[data-row-key]')].some(node => node.dataset.rowKey === 'item:head-arrival')",
                                    timeout=45_000,
                                )
                            retained = await page.locator(f"[data-row-key='{target_key}']").count()
                            assert retained == 1, (
                                "A loaded older item left the collection after tail membership changed"
                            )
                            assert await page.get_by_test_id("history-count").inner_text() == "60"

                            previous_page1_records = [
                                record for record in response_records if record.get("page") == "page-1"
                            ]
                            prior_handle = next(
                                (
                                    record["electricHandle"]
                                    for record in reversed(previous_page1_records)
                                    if record.get("electricHandle")
                                ),
                                None,
                            )
                            prior_offset = next(
                                (
                                    record["electricOffset"]
                                    for record in reversed(previous_page1_records)
                                    if record.get("electricOffset")
                                ),
                                None,
                            )
                            assert prior_handle is not None, previous_page1_records[-10:]
                            assert prior_offset is not None, previous_page1_records[-10:]
                            route_control["disconnectNextPage1Live"] = True
                            disconnect_trigger_cursor = base_cursor + 15
                            trigger_result = await apply_batch(
                                pool,
                                conversation_id="alpha-large",
                                source_id=SOURCE,
                                entries=[
                                    _text_entry(SOURCE, disconnect_trigger_cursor, "live-item", " +disconnect-trigger")
                                ],
                            )
                            assert trigger_result.row_writes == 1
                            await page.wait_for_function(
                                "(revision) => [...document.querySelectorAll('[data-row-key]')].some(node => node.dataset.rowKey === 'item:live-item' && node.dataset.textRevision === revision)",
                                arg=str(disconnect_trigger_cursor),
                                timeout=30_000,
                            )
                            await asyncio.wait_for(disconnect_failed.wait(), timeout=15)
                            disconnect_request = route_control["disconnectRequest"]
                            assert disconnect_request is not None, route_control
                            disconnected_query = parse_qs(urlsplit(disconnect_request["url"]).query)
                            assert disconnected_query.get("handle", [None])[-1] == prior_handle, disconnect_request
                            disconnect_offset = disconnected_query.get("offset", [None])[-1]
                            assert disconnect_offset is not None, disconnect_request
                            assert route_control["disconnectFailure"], route_control
                            await page.wait_for_function("navigator.onLine === false", timeout=1_000)
                            offline_probe = await page.evaluate(
                                """async () => {
                                  try {
                                    const response = await fetch('/api/checkpoint/alpha-large', {
                                      headers: { Authorization: `Bearer ${localStorage.getItem('spike-token')}` },
                                    })
                                    return { failed: false, status: response.status }
                                  } catch (error) {
                                    return { failed: true, error: String(error) }
                                  }
                                }"""
                            )
                            assert offline_probe["failed"], offline_probe
                            network_events.append({"event": "offline-probe", "result": offline_probe})
                            _write_browser_journal(
                                outputs / "browser-network-journal.json", network_events, response_records, page_errors
                            )
                            offline_batch = [
                                _text_entry(SOURCE, base_cursor + 16, "live-item", " +offline"),
                                _text_entry(SOURCE, base_cursor + 17, target_item_id, " +older-offline"),
                            ]
                            await apply_batch(
                                pool, conversation_id="alpha-large", source_id=SOURCE, entries=offline_batch
                            )
                            await asyncio.sleep(0.5)
                            offline_state = await page.evaluate(
                                """(targetKey) => {
                                  const live = [...document.querySelectorAll('[data-row-key]')]
                                    .find(node => node.dataset.rowKey === 'item:live-item')
                                  const target = [...document.querySelectorAll('[data-row-key]')]
                                    .find(node => node.dataset.rowKey === targetKey)
                                  return {
                                    liveTextRevision: live?.dataset.textRevision ?? null,
                                    targetRevision: target?.dataset.revision ?? null,
                                  }
                                }""",
                                arg=target_key,
                            )
                            assert offline_state == {
                                "liveTextRevision": str(disconnect_trigger_cursor),
                                "targetRevision": str(base_cursor + 2),
                            }, offline_state
                            before_resume_records = len(response_records)
                            before_resume_events = len(network_events)
                            await context.set_offline(False)
                            for opened in (page, page_two):
                                await opened.wait_for_function(
                                    "(revision) => [...document.querySelectorAll('[data-row-key]')].some(node => node.dataset.rowKey === 'item:live-item' && node.dataset.textRevision === revision)",
                                    arg=str(base_cursor + 16),
                                    timeout=60_000,
                                )
                            await page.wait_for_function(
                                "(expected) => [...document.querySelectorAll('[data-row-key]')].some(node => node.dataset.rowKey === expected.key && node.dataset.revision === expected.revision)",
                                arg={"key": target_key, "revision": str(base_cursor + 17)},
                                timeout=60_000,
                            )
                            await asyncio.gather(*tuple(response_tasks), return_exceptions=True)
                            resumed_records = response_records[before_resume_records:]
                            resumed_request = next(
                                (
                                    record
                                    for record in resumed_records
                                    if record.get("page") == "page-1"
                                    and record.get("query", {}).get("handle") == prior_handle
                                    and record.get("query", {}).get("offset") == disconnect_offset
                                ),
                                None,
                            )
                            assert resumed_request is not None, {
                                "expectedHandle": prior_handle,
                                "expectedOffset": disconnect_offset,
                                "observed": resumed_records[-20:],
                            }
                            resumed_event = next(
                                (
                                    event
                                    for event in network_events[before_resume_events:]
                                    if event.get("event") == "request"
                                    and event.get("page") == "page-1"
                                    and parse_qs(urlsplit(event["url"]).query).get("handle", [None])[-1] == prior_handle
                                    and parse_qs(urlsplit(event["url"]).query).get("offset", [None])[-1]
                                    == disconnect_offset
                                ),
                                None,
                            )
                            assert resumed_event is not None, {
                                "disconnectedRequest": disconnect_request,
                                "observed": network_events[before_resume_events:],
                            }
                            diagnostics["reconnect"] = {
                                "failedRequest": disconnect_request,
                                "failure": route_control["disconnectFailure"],
                                "offlineProbe": offline_probe,
                                "offlineState": offline_state,
                                "resumedRequest": resumed_event,
                                "sameHandle": parse_qs(urlsplit(resumed_event["url"]).query).get("handle", [None])[-1]
                                == prior_handle,
                                "sameOffset": parse_qs(urlsplit(resumed_event["url"]).query).get("offset", [None])[-1]
                                == disconnect_offset,
                            }
                            revision_history = await page.evaluate("window.__syncEvidence.revisions")
                            for key in ("item:live-item", target_key):
                                history = [int(value) for value in revision_history.get(key, [])]
                                assert all(left < right for left, right in pairwise(history)), (key, history)
                            await page.screenshot(path=outputs / "alpha-large-reconnected.png", full_page=True)

                            recovery_record_start = len(response_records)
                            route_control["recordResetSubsets"] = True
                            route_control["badHandle"] = True
                            rotation_cursor = base_cursor + 18
                            await apply_batch(
                                pool,
                                conversation_id="alpha-large",
                                source_id=SOURCE,
                                entries=[_text_entry(SOURCE, rotation_cursor, "live-item", " +rotate")],
                            )
                            await page.wait_for_function(
                                "(revision) => [...document.querySelectorAll('[data-row-key]')].some(node => node.dataset.rowKey === 'item:live-item' && node.dataset.textRevision === revision)",
                                arg=str(rotation_cursor),
                                timeout=60_000,
                            )
                            await page_two.wait_for_function(
                                "(revision) => [...document.querySelectorAll('[data-row-key]')].some(node => node.dataset.rowKey === 'item:live-item' && node.dataset.textRevision === revision)",
                                arg=str(rotation_cursor),
                                timeout=60_000,
                            )
                            assert route_control["badHandleRequest"] is not None, (
                                "No resumable Electric request was altered"
                            )
                            recovery_response = await asyncio.wait_for(recovery_responses.get(), timeout=60)
                            recovery_body = await recovery_response.body()
                            recovery_messages = [
                                message
                                for message in _messages(recovery_body)
                                if message.get("headers", {}).get("operation")
                            ]
                            recovery_query = parse_qs(urlsplit(recovery_response.url).query)
                            reset_rows = len(recovery_messages)
                            assert recovery_response.status == 200, {
                                "status": recovery_response.status,
                                "url": recovery_response.url,
                                "body": recovery_body[:1000].decode(errors="replace"),
                            }
                            assert recovery_query.get("offset") == ["-1"], recovery_query
                            assert not any(key.startswith("subset__") for key in recovery_query), recovery_query
                            assert reset_rows <= 30, {"recoveryResponse": recovery_response.url, "rows": reset_rows}
                            reissued_subset_responses: list[dict[str, Any]] = []
                            for _ in range(3):
                                subset_response = await asyncio.wait_for(reset_subset_responses.get(), timeout=45)
                                subset_body = await subset_response.body()
                                subset_messages = [
                                    message
                                    for message in _messages(subset_body)
                                    if message.get("headers", {}).get("operation")
                                ]
                                reissued_subset_responses.append(
                                    {
                                        "status": subset_response.status,
                                        "url": subset_response.url,
                                        "rowCount": len(subset_messages),
                                        "responseBytes": len(subset_body),
                                        "rowKeys": [
                                            message.get("value", {}).get("row_key") for message in subset_messages
                                        ],
                                    }
                                )
                            route_control["recordResetSubsets"] = False
                            assert all(record["status"] == 200 for record in reissued_subset_responses), (
                                reissued_subset_responses
                            )
                            assert all(record["rowCount"] <= 30 for record in reissued_subset_responses), (
                                reissued_subset_responses
                            )
                            await asyncio.gather(*tuple(response_tasks), return_exceptions=True)
                            reset_records = [
                                record
                                for record in response_records[recovery_record_start:]
                                if record.get("page") == "page-1" and record.get("path") == "/api/electric/alpha-large"
                            ]
                            stale_records = [record for record in reset_records if record.get("status") == 409]
                            assert stale_records, reset_records[-20:]
                            assert any("must-refetch" in record.get("controls", []) for record in stale_records), (
                                stale_records
                            )
                            post_reset_state = await page.evaluate(
                                """(targetKey) => {
                                  const rows = [...document.querySelectorAll('[data-row-key]')]
                                  const target = rows.find(node => node.dataset.rowKey === targetKey)
                                  const live = rows.find(node => node.dataset.rowKey === 'item:live-item')
                                  return {
                                    historyCount: Number(document.querySelector('[data-testid=history-count]')?.textContent ?? '-1'),
                                    tailCount: Number(document.querySelector('[data-testid=tail-count]')?.textContent ?? '-1'),
                                    targetRevision: target?.dataset.revision ?? null,
                                    targetStatus: target?.dataset.status ?? null,
                                    liveTextRevision: live?.dataset.textRevision ?? null,
                                    loadedRowCount: rows.length,
                                  }
                                }""",
                                arg=target_key,
                            )
                            history_preserved = (
                                post_reset_state["historyCount"] == 60
                                and post_reset_state["targetRevision"] == str(base_cursor + 17)
                                and post_reset_state["targetStatus"] == "complete"
                                and post_reset_state["liveTextRevision"] == str(rotation_cursor)
                            )
                            await page.screenshot(path=outputs / "alpha-large-stale-handle.png", full_page=True)
                            diagnostics["mustRefetch"] = {
                                "forcedRequest": route_control["badHandleRequest"],
                                "responses": stale_records,
                                "recoveryResponse": {
                                    "status": recovery_response.status,
                                    "query": {key: values[-1] for key, values in recovery_query.items()},
                                    "rowCount": reset_rows,
                                    "responseBytes": len(recovery_body),
                                    "subsetFiltersApplied": any(key.startswith("subset__") for key in recovery_query),
                                },
                                "reissuedSubsetResponses": reissued_subset_responses,
                                "postResetState": post_reset_state,
                                "historyRowsPreserved": history_preserved,
                                "boundedReset": reset_rows <= 30
                                and all(record.get("rowCount", 0) <= 30 for record in reissued_subset_responses),
                                "persistentCacheTagRecovery": "not exercised; no persisted browser cache or tags configured",
                            }
                            _write_json(outputs / "must-refetch-evidence.json", diagnostics["mustRefetch"])
                            assert history_preserved, post_reset_state
                            await page.goto(f"{app_url}/?conversation=alpha-large", wait_until="domcontentloaded")
                            await _wait_ready(page)

                            old_electric_request_records = len(response_records)
                            electric.get_wrapped_container().stop(timeout=10)
                            restarted = (
                                LoggedContainer(electric_1_8.IMAGE.tag, test_name="electric-derisk-electric-restart")
                                .with_network(network)
                                .with_network_aliases("electric")
                                .with_exposed_ports(3000)
                                .with_env(
                                    "DATABASE_URL",
                                    "postgresql://postgres:postgres@postgres:5432/postgres?sslmode=disable",
                                )
                                .with_env("ELECTRIC_INSECURE", "true")
                            )
                            with restarted:
                                restarted_url = (
                                    f"http://{restarted.get_container_host_ip()}:{restarted.get_exposed_port(3000)}"
                                )
                                await _wait_electric(restarted_url)
                                proxy_one.state.electric_url = f"{restarted_url}/v1/shape"
                                proxy_two.state.electric_url = f"{restarted_url}/v1/shape"
                                restart_cursor = base_cursor + 19
                                await apply_batch(
                                    pool,
                                    conversation_id="alpha-large",
                                    source_id=SOURCE,
                                    entries=[_text_entry(SOURCE, restart_cursor, "live-item", " +restart")],
                                )
                                await page.wait_for_function(
                                    "(revision) => [...document.querySelectorAll('[data-row-key]')].some(node => node.dataset.rowKey === 'item:live-item' && node.dataset.textRevision === revision)",
                                    arg=str(restart_cursor),
                                    timeout=60_000,
                                )
                                await page_two.wait_for_function(
                                    "(revision) => [...document.querySelectorAll('[data-row-key]')].some(node => node.dataset.rowKey === 'item:live-item' && node.dataset.textRevision === revision)",
                                    arg=str(restart_cursor),
                                    timeout=60_000,
                                )
                                await page.screenshot(
                                    path=outputs / "alpha-large-electric-restarted.png", full_page=True
                                )
                                wal_state = await pool.fetchrow("""SELECT current_setting('wal_level') AS wal_level,
                                    (SELECT count(*) FROM pg_replication_slots WHERE slot_type='logical') AS logical_slots,
                                    (SELECT count(*) FROM pg_replication_slots WHERE slot_type='logical' AND active) AS active_slots,
                                    pg_wal_lsn_diff(pg_current_wal_lsn(), COALESCE((SELECT min(restart_lsn) FROM pg_replication_slots WHERE slot_type='logical'), pg_current_wal_lsn()))::bigint AS retained_wal_bytes""")
                                assert wal_state is not None
                                diagnostics["postgresReplication"] = dict(wal_state)
                                diagnostics["postgresReplication"]["retained_wal_bytes"] = int(
                                    wal_state["retained_wal_bytes"]
                                )
                                assert wal_state["wal_level"] == "logical"
                                assert int(wal_state["logical_slots"]) >= 1
                                assert int(wal_state["active_slots"]) >= 1
                                diagnostics["restart"] = {
                                    "completed": True,
                                    "page1Responses": response_records[old_electric_request_records:],
                                    "postgresReplication": diagnostics["postgresReplication"],
                                }
                                _write_json(outputs / "electric-restart-evidence.json", diagnostics["restart"])

                            final_checkpoint = await _api_get(app_url, "/api/checkpoint/alpha-large", headers=AUTH)
                            assert final_checkpoint.status_code == 200
                            assert final_checkpoint.json()["throughCursor"] == str(restart_cursor)

                            current_text = (
                                "seed text +during-history +first +second +disconnect-trigger +offline +rotate +restart"
                            )
                            current_text_ref = await pool.fetchval(
                                "SELECT text_payload_ref FROM sync_view_row WHERE conversation_id='alpha-large' AND row_key='item:live-item'"
                            )
                            assert isinstance(current_text_ref, str)
                            await page.get_by_role("button", name="Open text").click()
                            reopened_text = await _assert_rendered_payload(
                                page,
                                expected=current_text,
                                payload_ref=current_text_ref,
                                revision=restart_cursor,
                            )
                            text_shape_ref = reopened_text["shapeRef"]
                            assert text_shape_ref == current_text_ref
                            await page.screenshot(path=outputs / "alpha-large-final.png", full_page=True)

                            streaming_groups: list[dict[str, Any]] = []
                            streaming_chunks = [f"<stream-chunk-{index:02d}>" for index in range(32)]
                            next_stream_cursor = restart_cursor + 1
                            for group_start in range(0, len(streaming_chunks), 4):
                                group_chunks = streaming_chunks[group_start : group_start + 4]
                                group_entries = [
                                    _text_entry(
                                        SOURCE,
                                        next_stream_cursor + index,
                                        "live-item",
                                        chunk,
                                    )
                                    for index, chunk in enumerate(group_chunks)
                                ]
                                group_result = await apply_batch(
                                    pool,
                                    conversation_id="alpha-large",
                                    source_id=SOURCE,
                                    entries=group_entries,
                                )
                                final_group_cursor = next_stream_cursor + len(group_entries) - 1
                                assert group_result == ApplyResult(final_group_cursor, 0, 1, 4), group_result
                                current_text += "".join(group_chunks)
                                latest_text_ref = await pool.fetchval(
                                    "SELECT text_payload_ref FROM sync_view_row WHERE conversation_id='alpha-large' AND row_key='item:live-item'"
                                )
                                group_visible = await _assert_rendered_payload(
                                    page,
                                    expected=current_text,
                                    payload_ref=latest_text_ref,
                                    revision=final_group_cursor,
                                )
                                assert int(group_visible["contentBytes"]) == len(current_text.encode("utf-8"))
                                streaming_groups.append(
                                    {
                                        "throughCursor": final_group_cursor,
                                        "appendedChunks": len(group_chunks),
                                        "renderedChunkCount": group_visible["chunkCount"],
                                        "renderedContentBytes": group_visible["contentBytes"],
                                        "contentSha256": group_visible["contentSha256"],
                                    }
                                )
                                next_stream_cursor = final_group_cursor + 1

                            assert next_stream_cursor == restart_cursor + 33
                            stream_end_cursor = next_stream_cursor - 1
                            await page_two.wait_for_function(
                                "(revision) => document.querySelector('[data-row-key=\"item:live-item\"]')?.dataset.textRevision === revision",
                                arg=str(stream_end_cursor),
                                timeout=45_000,
                            )
                            await asyncio.gather(*tuple(response_tasks), return_exceptions=True)
                            live_stream_records = [
                                record
                                for record in response_records
                                if record.get("page") == "page-1"
                                and record.get("payloadRef") == text_shape_ref
                                and record.get("payloadPart") == "chunks"
                                and record.get("status") == 200
                            ]
                            streamed_rows = [
                                row for record in live_stream_records for row in record.get("chunkRows", [])
                            ]
                            streamed_rows.sort(key=lambda row: row["chunkIndex"])
                            expected_streamed_chunk_count = reopened_text["chunkCount"] + len(streaming_chunks)
                            assert len(streamed_rows) == expected_streamed_chunk_count, {
                                "shapeRef": text_shape_ref,
                                "expectedRows": expected_streamed_chunk_count,
                                "observedRows": len(streamed_rows),
                                "records": live_stream_records,
                            }
                            assert [row["chunkIndex"] for row in streamed_rows] == list(
                                range(expected_streamed_chunk_count)
                            ), streamed_rows
                            assert len({row["contentSha256"] for row in streamed_rows}) == len(streamed_rows), (
                                "A chunk was retransmitted or replaced inside one immutable generation",
                                streamed_rows,
                            )
                            streamed_content_bytes = sum(int(row["contentBytes"]) for row in streamed_rows)
                            assert streamed_content_bytes == len(current_text.encode("utf-8")), {
                                "contentBytes": streamed_content_bytes,
                                "expectedBytes": len(current_text.encode("utf-8")),
                            }
                            streaming_evidence = {
                                "shapeRef": text_shape_ref,
                                "groups": streaming_groups,
                                "snapshotAndLiveRowCount": len(streamed_rows),
                                "contentBytesTransferred": streamed_content_bytes,
                                "networkResponseBytes": sum(record["responseBytes"] for record in live_stream_records),
                                "chunkIndexes": [row["chunkIndex"] for row in streamed_rows],
                            }
                            await page.screenshot(path=outputs / "alpha-large-streamed-chunks.png", full_page=True)

                            text_requests_before_close = len(
                                [
                                    request
                                    for request in (await _api_get(app_url, "/api/proxy-metrics", headers=AUTH)).json()[
                                        "payloadRequests"
                                    ]
                                    if request["field"] == "text"
                                    and request["itemId"] == "live-item"
                                    and request["part"] == "chunks"
                                ]
                            )
                            await page.get_by_role("button", name="Close payload").click()
                            await page.wait_for_function(
                                """(shapeRef) => window.__syncEvidence.retiredPayloadCollections.some(entry =>
                                  entry.shapeRef === shapeRef && entry.sizeAfterCleanup === 0
                                  && entry.subscribersAfterCleanup === 0 && entry.statusAfterCleanup === 'cleaned-up')""",
                                arg=text_shape_ref,
                                timeout=30_000,
                            )
                            replacement_cursor = stream_end_cursor + 1
                            text_replacement_result = await apply_batch(
                                pool,
                                conversation_id="alpha-large",
                                source_id=SOURCE,
                                entries=[
                                    _entry(
                                        SOURCE,
                                        replacement_cursor,
                                        "item_completed",
                                        event_pb2.ItemCompleted(item_id="live-item", text="AUTHORITATIVE_FINAL"),
                                    )
                                ],
                            )
                            assert text_replacement_result == ApplyResult(replacement_cursor, 0, 1, 1)
                            final_text_ref = await pool.fetchval(
                                "SELECT text_payload_ref FROM sync_view_row WHERE conversation_id='alpha-large' AND row_key='item:live-item'"
                            )
                            await page.wait_for_function(
                                "(revision) => document.querySelector('[data-row-key=\"item:live-item\"]')?.dataset.textRevision === revision",
                                arg=str(replacement_cursor),
                                timeout=45_000,
                            )
                            payload_after_text_close = (await _api_get(
                                app_url, "/api/proxy-metrics", headers=AUTH
                            )).json()["payloadRequests"]
                            assert len(
                                [
                                    request
                                    for request in payload_after_text_close
                                    if request["field"] == "text"
                                    and request["itemId"] == "live-item"
                                    and request["part"] == "chunks"
                                ]
                            ) == text_requests_before_close
                            await page.get_by_role("button", name="Open text").click()
                            final_text_visible = await _assert_rendered_payload(
                                page,
                                expected="AUTHORITATIVE_FINAL",
                                payload_ref=final_text_ref,
                                revision=replacement_cursor,
                            )
                            assert final_text_visible["shapeRef"] == final_text_ref
                            assert final_text_visible["chunkCount"] == 1
                            final_manifest = await pool.fetchrow(
                                """SELECT source_id,generation_id,revision,chunk_count,content_bytes
                                   FROM projected_payload_manifest WHERE conversation_id='alpha-large' AND payload_ref=$1""",
                                final_text_ref,
                            )
                            assert final_manifest is not None
                            live_current_probe = {
                                "itemId": "live-item",
                                "fieldName": "text",
                                "sourceId": final_manifest["source_id"],
                                "generationId": final_manifest["generation_id"],
                            }
                            await pool.execute("ANALYZE projected_payload_chunk")
                            live_current_plan = await _payload_chunk_plan(
                                pool, live_current_probe, conversation_id="alpha-large"
                            )
                            _write_json(outputs / "live-current-generation-query-plan.json", live_current_plan)
                            live_plan_nodes = _flatten_plan(live_current_plan[0]["Plan"])
                            assert any(
                                node.get("Index Name") == "projected_payload_chunk_pkey"
                                and node.get("Actual Rows") == 1
                                for node in live_plan_nodes
                            ), live_plan_nodes
                            await asyncio.gather(*tuple(response_tasks), return_exceptions=True)
                            final_payload_records = [
                                record
                                for record in response_records
                                if record.get("page") == "page-1"
                                and record.get("payloadRef") == final_text_ref
                                and record.get("payloadPart") == "chunks"
                                and record.get("status") == 200
                            ]
                            final_payload_rows = [
                                row for record in final_payload_records for row in record.get("chunkRows", [])
                            ]
                            assert len(final_payload_rows) == 1, final_payload_records
                            diagnostics["payloadRevisionLifecycle"] = {
                                "streaming": streaming_evidence,
                                "closedTextChunkRequestsStayedAt": text_requests_before_close,
                                "replacementCursor": replacement_cursor,
                                "replacement": final_text_visible,
                                "replacementSnapshotRows": len(final_payload_rows),
                                "replacementSnapshotNetworkBytes": sum(
                                    record["responseBytes"] for record in final_payload_records
                                ),
                                "replacementQueryPlan": live_current_plan,
                            }
                            _write_json(
                                outputs / "payload-lifecycle-evidence.json",
                                diagnostics["payloadRevisionLifecycle"],
                            )

                            reconstruction_results: dict[str, Any] = {}
                            for probe_name in ("payload-small", "payload-large"):
                                probe = payload_probes[probe_name]
                                await page.goto(
                                    f"{app_url}/?conversation=alpha-small&payloadItem={probe_name}",
                                    wait_until="domcontentloaded",
                                )
                                await _wait_ready(page, exact_bigint=True)
                                await page.get_by_role("button", name="Open text").click()
                                rendered_probe = await _assert_rendered_payload(
                                    page,
                                    expected=probe["content"],
                                    payload_ref=probe["payloadRef"],
                                    revision=probe["anchor"],
                                )
                                assert rendered_probe["chunkCount"] == probe["chunkCount"], rendered_probe
                                assert int(rendered_probe["contentBytes"]) == probe["contentBytes"]
                                await asyncio.gather(*tuple(response_tasks), return_exceptions=True)
                                probe_records = [
                                    record
                                    for record in response_records
                                    if record.get("page") == "page-1"
                                    and record.get("payloadRef") == probe["payloadRef"]
                                    and record.get("payloadPart") == "chunks"
                                    and record.get("status") == 200
                                ]
                                probe_rows = [
                                    row for record in probe_records for row in record.get("chunkRows", [])
                                ]
                                probe_rows.sort(key=lambda row: row["chunkIndex"])
                                assert [row["chunkIndex"] for row in probe_rows] == list(
                                    range(probe["chunkCount"])
                                ), {"probe": probe, "records": probe_records}
                                assert sum(int(row["contentBytes"]) for row in probe_rows) == probe["contentBytes"]
                                stored_history_rows = await pool.fetchval(
                                    """SELECT count(*) FROM projected_payload_chunk
                                       WHERE conversation_id='alpha-small' AND item_id=$1 AND source_id=$2""",
                                    probe["itemId"],
                                    probe["sourceId"],
                                )
                                assert stored_history_rows == probe["historyChunks"] + probe["chunkCount"]
                                reconstruction_results[probe_name] = {
                                    "rendered": rendered_probe,
                                    "supersededGenerationsInDatabase": probe["historyChunks"],
                                    "currentGenerationRowsOnWire": len(probe_rows),
                                    "contentBytesOnWire": sum(int(row["contentBytes"]) for row in probe_rows),
                                    "networkResponseBytes": sum(record["responseBytes"] for record in probe_records),
                                    "queryPlan": payload_plans[probe_name],
                                }
                                await page.screenshot(
                                    path=outputs / f"{probe_name}-exact-revision.png", full_page=True
                                )
                            assert reconstruction_results["payload-small"]["currentGenerationRowsOnWire"] == 2
                            assert reconstruction_results["payload-large"]["currentGenerationRowsOnWire"] == 32
                            assert reconstruction_results["payload-small"]["supersededGenerationsInDatabase"] == 12
                            assert reconstruction_results["payload-large"]["supersededGenerationsInDatabase"] == 5_000
                            diagnostics["boundedReconstruction"] = reconstruction_results
                            _write_json(outputs / "payload-reconstruction-evidence.json", reconstruction_results)

                            gateway_dispatches = await _api_get(app_url, "/api/proxy-metrics", headers=AUTH)
                            assert gateway_dispatches.status_code == 200
                            dispatches = gateway_dispatches.json()["dispatches"]
                            assert dispatches.count("proxy-1") > 0
                            assert dispatches.count("proxy-2") > 0
                            assert all(left != right for left, right in pairwise(dispatches)), dispatches
                            diagnostics["proxy"] = {
                                "dispatches": dispatches,
                                "proxy1RequestCount": proxy_one.state.request_count,
                                "proxy2RequestCount": proxy_two.state.request_count,
                            }

                            payload_metrics = gateway_dispatches.json()["payloadRequests"]
                            assert any(request["field"] == "text" for request in payload_metrics)
                            assert any(request["field"] == "arguments" for request in payload_metrics)
                            assert any(request["field"] == "output" for request in payload_metrics)
                            assert not any(request["field"] == "reasoning" for request in payload_metrics)
                            main_view_records = [
                                record
                                for record in response_records
                                if record.get("path") in {"/api/electric/alpha-small", "/api/electric/alpha-large"}
                            ]
                            assert all(not record.get("containsSensitivePayload") for record in main_view_records)
                            assert all(
                                not (
                                    set(record.get("fields", []))
                                    & {"text", "arguments", "output", "reasoning", "content"}
                                )
                                for record in main_view_records
                            ), main_view_records
                            payload_chunk_records = [
                                record for record in response_records if record.get("payloadPart") == "chunks"
                            ]
                            assert all(
                                request["field"] in {"text", "arguments", "output"}
                                for request in payload_metrics
                                if request["part"] == "chunks"
                            ), payload_metrics
                            assert payload_chunk_records, response_records
                            assert not page_errors, page_errors
                            diagnostics["payload"] = payload_metrics
                            diagnostics["browser"] = {
                                "smallBootstrapRows": small_rows,
                                "smallBootstrapBytes": small_bytes,
                                "largeBootstrapRows": large_rows,
                                "largeBootstrapBytes": large_bytes,
                                "firstHistoryCursor": min_history_anchor,
                                "exclusiveBeforeProof": max(int(row["anchor"]) for row in new_history)
                                < min_history_anchor,
                                "scrollAnchor": scroll_evidence,
                                "pageErrors": page_errors,
                            }
                            _write_json(outputs / "network-evidence.json", response_records)
                            _write_json(outputs / "e2e-summary.json", diagnostics)
                            await context.tracing.stop(path=outputs / "browser-trace.zip")
                            await context.close()
                            await browser.close()
            finally:
                await pool.close()


if __name__ == "__main__":
    pytest_bazel.main()
