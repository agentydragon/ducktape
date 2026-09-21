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
from x.agentplane_sync.projector import ApplyResult, UnknownItemError, apply_batch, initialize_database
from x.agentplane_sync.service import SubsetGate, create_electric_proxy, create_gateway

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


async def _seed_large_live_rows(pool: asyncpg.Pool) -> int:
    base = HIGH_CURSOR + LARGE_COUNT + 100
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
            "pending",
            None,
            "sync-command",
        ),
    ]
    columns = (
        "conversation_id,row_key,entity_kind,anchor,revision,item_id,item_kind,tool_name,"
        "text_revision,arguments_revision,output_revision,reasoning_revision,"
        "text_bytes,arguments_bytes,output_bytes,reasoning_bytes,status,model,command_id"
    )
    await pool.executemany(
        f"INSERT INTO sync_view_row ({columns}) VALUES (" + ",".join(f"${index}" for index in range(1, 20)) + ")", rows
    )
    await pool.executemany(
        """INSERT INTO projected_payload_part
           (conversation_id,item_id,field_name,source_cursor,operation,content)
           VALUES ('alpha-large',$1,$2,$3,'replace',$4)""",
        [
            ("live-item", "text", base - 4, "seed text"),
            ("tool-row", "arguments", base - 3, ""),
            ("tool-row", "output", base - 3, "SENSITIVE_INITIAL_OUTPUT"),
            ("reasoning-row", "reasoning", base - 2, "SENSITIVE_INITIAL_REASONING"),
        ],
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
    payloads_before = await pool.fetchval(
        "SELECT count(*) FROM projected_payload_part WHERE conversation_id='race-thread'"
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
    after_failure_payloads = await pool.fetchval(
        "SELECT count(*) FROM projected_payload_part WHERE conversation_id='race-thread'"
    )
    assert after_failure is not None
    assert after_failure_row is not None
    assert int(after_failure["through_cursor"]) == 2
    assert dict(after_failure_row) == dict(before)
    assert after_failure_payloads == payloads_before
    retried = await apply_batch(pool, conversation_id="race-thread", source_id="runner-race", entries=[failed_batch[0]])
    assert retried.through_cursor == 3
    assert retried.row_writes == 1
    assert retried.payload_parts == 1
    return {
        "parallelResults": [result.__dict__ for result in parallel],
        "failedCheckpoint": int(after_failure["through_cursor"]),
        "payloadsBeforeFailure": payloads_before,
        "payloadsAfterFailure": after_failure_payloads,
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
        if "/api/electric/" not in response.url:
            return
        try:
            body = await response.body()
        except Exception as error:
            records.append({"page": page_name, "url": response.url, "error": str(error)})
            write_journal()
            return
        request_parts = urlsplit(request.url)
        query = parse_qs(request_parts.query)
        messages = _messages(body)
        row_messages = [message for message in messages if message.get("headers", {}).get("operation")]
        post_body = _parse_body(request.post_data.encode()) if request.post_data else {}
        row_values = [message.get("value", {}) for message in row_messages]
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
        if "/api/electric/" in response.url:
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
        if "/api/electric/" in request.url:
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
        if "/api/electric/" in request.url:
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
                    proxy_one = create_electric_proxy(electric_shape_url, "proxy-1")
                    proxy_two = create_electric_proxy(electric_shape_url, "proxy-2")
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
                        diagnostics["auth"] = {
                            "crossConversation": denied.status_code,
                            "getTableOverride": get_override.status_code,
                            "postTableOverride": post_override.status_code,
                            "whereConversationOverride": where_override.status_code,
                        }

                        route_control: dict[str, Any] = {
                            "badHandle": False,
                            "badHandleRequest": None,
                            "disconnectNextPage1Live": False,
                            "disconnectRequest": None,
                            "disconnectFailure": None,
                            "recordResetSubsets": False,
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

                            await context.route("**/api/electric/**", route_request)
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

                            await page.get_by_role("button", name="Open text").click()
                            await page.wait_for_function(
                                "document.querySelector('[data-testid=payload-body]')?.textContent === 'seed text +during-history'",
                                timeout=30_000,
                            )
                            payload_requests = (await _api_get(app_url, "/api/proxy-metrics", headers=AUTH)).json()[
                                "payloadRequests"
                            ]
                            assert [request["field"] for request in payload_requests] == ["text"]

                            main_batch = [
                                _text_entry(SOURCE, base_cursor + 3, "live-item", " +first"),
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
                            assert main_result == ApplyResult(base_cursor + 8, 0, 4, 5), main_result
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
                            text_result = await page.wait_for_function(
                                "document.querySelector('[data-testid=payload-body]')?.textContent === 'seed text +during-history +first +second'",
                                timeout=30_000,
                            )
                            assert text_result
                            await page.get_by_role("button", name="Open arguments").click()
                            await page.wait_for_function(
                                "document.querySelector('[data-testid=payload-body]')?.textContent === '{\"x\":1}'",
                                timeout=30_000,
                            )
                            await page.get_by_role("button", name="Open output").click()
                            await page.wait_for_function(
                                "document.querySelector('[data-testid=payload-body]')?.textContent === 'SENSITIVE_INITIAL_OUTPUTSENSITIVE_OUTPUT_AFTER'",
                                timeout=30_000,
                            )
                            await page.get_by_role("button", name="Close payload").click()
                            output_requests_before_close = len(
                                [
                                    request
                                    for request in (await _api_get(app_url, "/api/proxy-metrics", headers=AUTH)).json()[
                                        "payloadRequests"
                                    ]
                                    if request["field"] == "output"
                                ]
                            )

                            closed_interest_batch = [
                                _text_entry(SOURCE, base_cursor + 9, "live-item", " +closed"),
                                _entry(
                                    SOURCE,
                                    base_cursor + 10,
                                    "tool_output_delta",
                                    event_pb2.ToolOutputDelta(item_id="tool-row", text="SENSITIVE_OUTPUT_CLOSED"),
                                ),
                            ]
                            closed_result = await apply_batch(
                                pool, conversation_id="alpha-large", source_id=SOURCE, entries=closed_interest_batch
                            )
                            assert closed_result.row_writes == 2
                            assert closed_result.payload_parts == 2
                            await page.wait_for_function(
                                "(revision) => [...document.querySelectorAll('[data-row-key]')].some(node => node.dataset.rowKey === 'item:live-item' && node.dataset.textRevision === revision)",
                                arg=str(base_cursor + 9),
                                timeout=45_000,
                            )
                            payload_after_close = (await _api_get(app_url, "/api/proxy-metrics", headers=AUTH)).json()[
                                "payloadRequests"
                            ]
                            assert (
                                len([request for request in payload_after_close if request["field"] == "output"])
                                == output_requests_before_close
                            )
                            assert await page.get_by_test_id("payload-body").inner_text() == ""
                            await page.screenshot(path=outputs / "alpha-large-updated.png", full_page=True)

                            head_cursor = base_cursor + 11
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
                            disconnect_trigger_cursor = base_cursor + 12
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
                                _text_entry(SOURCE, base_cursor + 13, "live-item", " +offline"),
                                _text_entry(SOURCE, base_cursor + 14, target_item_id, " +older-offline"),
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
                                    arg=str(base_cursor + 13),
                                    timeout=60_000,
                                )
                            await page.wait_for_function(
                                "(expected) => [...document.querySelectorAll('[data-row-key]')].some(node => node.dataset.rowKey === expected.key && node.dataset.revision === expected.revision)",
                                arg={"key": target_key, "revision": str(base_cursor + 14)},
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
                            rotation_cursor = base_cursor + 15
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
                                and post_reset_state["targetRevision"] == str(base_cursor + 14)
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
                                restart_cursor = base_cursor + 16
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

                            for opened in (page, page_two):
                                await opened.get_by_role("button", name="Open text").click()
                                await opened.wait_for_function(
                                    "(expected) => document.querySelector('[data-testid=payload-body]')?.textContent === expected",
                                    arg="seed text +during-history +first +second +closed +disconnect-trigger +offline +rotate +restart",
                                    timeout=45_000,
                                )
                            await page.screenshot(path=outputs / "alpha-large-final.png", full_page=True)
                            await asyncio.gather(*tuple(response_tasks), return_exceptions=True)

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
                            assert all(not record.get("containsSensitivePayload") for record in response_records)
                            assert all(
                                not (
                                    set(record.get("fields", []))
                                    & {"text", "arguments", "output", "reasoning", "content"}
                                )
                                for record in response_records
                            ), response_records
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
