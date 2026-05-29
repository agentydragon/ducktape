from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

from playwright.async_api import Browser, Page, Playwright


@dataclass
class BrowserSignals:
    capture_response_bodies: bool = False
    console_details: list[dict[str, Any]] = field(default_factory=list)
    console_messages: list[str] = field(default_factory=list)
    failed_requests: list[dict[str, Any]] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    request_failures: list[dict[str, Any]] = field(default_factory=list)
    request_finishes: list[dict[str, Any]] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    response_bodies: list[dict[str, Any]] = field(default_factory=list)
    responses: list[dict[str, Any]] = field(default_factory=list)
    pending_tasks: set[asyncio.Task] = field(default_factory=set)


async def launch_chromium_for_proxy(playwright: Playwright, config) -> Browser:
    return await playwright.chromium.launch(
        executable_path=chromium_executable(),
        headless=True,
        args=[
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-setuid-sandbox",
            "--ignore-certificate-errors",
            "--no-sandbox",
            f"--proxy-server=http://{config.proxy_host}:{config.proxy_port}",
        ],
    )


def collect_browser_signals(page: Page) -> BrowserSignals:
    signals = BrowserSignals(capture_response_bodies=bool(os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR")))
    request_ids: dict[Any, int] = {}
    next_request_id = 1
    next_response_id = 1

    async def describe_console_message(message):
        args = []
        for arg in message.args:
            try:
                args.append(
                    await arg.evaluate("value => JSON.parse(JSON.stringify(value, Object.getOwnPropertyNames(value)))")
                )
            except Exception as error:
                args.append({"unserializable": str(error)})
        signals.console_details.append({"args": args, "text": message.text, "type": message.type})

    def on_console(message):
        signals.console_messages.append(f"[{message.type}] {message.text}")
        track_task(signals, describe_console_message(message))

    def on_request(request):
        nonlocal next_request_id
        request_ids[request] = next_request_id
        next_request_id += 1
        signals.requests.append(
            {
                "headers": request.headers,
                "id": request_ids[request],
                "isNavigationRequest": request.is_navigation_request(),
                "method": request.method,
                "postData": request.post_data,
                "resourceType": request.resource_type,
                "url": request.url,
            }
        )

    def on_request_failed(request):
        record = {"errorText": request.failure or "unknown", "requestId": request_ids.get(request), "url": request.url}
        signals.failed_requests.append(record)
        signals.request_failures.append(record)

    def on_request_finished(request):
        signals.request_finishes.append({"requestId": request_ids.get(request), "url": request.url})

    def on_response(response):
        nonlocal next_response_id
        request = response.request
        record = {
            "fromServiceWorker": response.from_service_worker,
            "headers": response.headers,
            "id": next_response_id,
            "requestId": request_ids.get(request),
            "resourceType": request.resource_type,
            "status": response.status,
            "statusText": response.status_text,
            "url": response.url,
        }
        next_response_id += 1
        signals.responses.append(record)
        if signals.capture_response_bodies:
            track_task(signals, capture_response_body(response, record, signals))

    page.on("console", on_console)
    page.on("pageerror", lambda error: signals.page_errors.append(str(error)))
    page.on("request", on_request)
    page.on("requestfailed", on_request_failed)
    page.on("requestfinished", on_request_finished)
    page.on("response", on_response)
    return signals


def track_task(signals: BrowserSignals, coroutine) -> None:
    task = asyncio.create_task(coroutine)
    signals.pending_tasks.add(task)
    task.add_done_callback(signals.pending_tasks.discard)


async def capture_response_body(response, record: dict[str, Any], signals: BrowserSignals) -> None:
    try:
        body = await response.body()
    except Exception as error:
        record["bodyError"] = str(error)
        return
    file_name = response_body_file_name(record)
    record["body"] = {"byteLength": len(body), "fileName": file_name, "sha256": sha256(body).hexdigest()}
    signals.response_bodies.append({"buffer": body, "fileName": file_name, "responseId": record["id"]})


def response_body_file_name(record: dict[str, Any]) -> str:
    path = "/response"
    try:
        path = urlsplit(record["url"]).path or "/index.html"
    except Exception:
        path = f"/{record.get('resourceType') or 'response'}"
    basename = "".join(char if char.isalnum() or char in "._-" else "_" for char in Path(path).name)[:80] or "response"
    return f"{record['id']:04d}-{record['status']}-{record.get('resourceType')}-{basename}"


def collect_console_errors(signals: BrowserSignals, ignored_patterns=()) -> list[str]:
    ignored = compile_patterns(ignored_patterns)
    return [
        message
        for message in signals.console_messages
        if message.startswith("[error] ") and not any(pattern.search(message) for pattern in ignored)
    ]


def collect_ignored_console_errors(signals: BrowserSignals, ignored_patterns=()) -> list[str]:
    ignored = compile_patterns(ignored_patterns)
    return [
        message
        for message in signals.console_messages
        if message.startswith("[error] ") and any(pattern.search(message) for pattern in ignored)
    ]


def compile_patterns(patterns):
    return [pattern if hasattr(pattern, "search") else re.compile(pattern) for pattern in patterns]


def assert_no_console_errors(signals: BrowserSignals, detail: str, ignored_patterns=()) -> None:
    console_errors = collect_console_errors(signals, ignored_patterns)
    assert console_errors == [], f"console errors:\n{chr(10).join(console_errors)}\n{detail}"


async def settle_pending_tasks(signals: BrowserSignals, timeout_s: float = 1.0) -> None:
    if not signals.pending_tasks:
        return
    await asyncio.wait(signals.pending_tasks, timeout=timeout_s)


async def collect_failure_diagnostics(page: Page, signals: BrowserSignals, read_page_state=None) -> str:
    await settle_pending_tasks(signals)
    page_state = await safe_read_page_state(page, read_page_state)
    return "\n".join(
        [
            "browser diagnostics:",
            f"page errors:\n{chr(10).join(signals.page_errors) or '<none>'}",
            f"console:\n{chr(10).join(signals.console_messages) or '<none>'}",
            f"console details:\n{format_json(signals.console_details)}",
            f"failed requests:\n{format_json(signals.failed_requests)}",
            f"recent responses:\n{format_json(signals.responses[-80:])}",
            f"page state:\n{format_json(page_state)}",
        ]
    )


async def safe_read_page_state(page: Page, read_page_state=None) -> dict[str, Any]:
    try:
        if read_page_state:
            return cast(dict[str, Any], await read_page_state(page))
        base = cast(
            dict[str, Any],
            await page.evaluate(
                """() => ({
                    bodyText: document.body?.innerText ?? "",
                    documentReadyState: document.readyState,
                    html: document.documentElement?.outerHTML ?? "",
                    liveProxyActive: globalThis.__jsDebundleLiveProxy?.active === true,
                    liveProxyMarker: document.documentElement.dataset.jsDebundleLiveProxy ?? null,
                    location: location.href,
                    title: document.title,
                })"""
            ),
        )
        base["bodyText"] = base.get("bodyText", "")[:3000]
        base["html"] = base.get("html", "")[:5000]
        return base
    except Exception as error:
        return {"evaluateError": str(error)}


async def write_undeclared_outputs(
    page: Page,
    signals: BrowserSignals,
    error: BaseException | None,
    *,
    ignored_console_patterns=(),
    read_page_state=None,
) -> Path | None:
    output_dir_raw = os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR")
    if not output_dir_raw:
        return None
    output_dir = Path(output_dir_raw)
    output_dir.mkdir(parents=True, exist_ok=True)
    await settle_pending_tasks(signals, timeout_s=5.0)
    page_state = await safe_read_page_state(page, read_page_state)
    response_body_dir = output_dir / "response_bodies"
    response_body_dir.mkdir(parents=True, exist_ok=True)
    for body in signals.response_bodies:
        (response_body_dir / body["fileName"]).write_bytes(body["buffer"])

    write_json(
        output_dir / "summary.json",
        {
            "result": {"ok": error is None, "error": serialize_error(error) if error else None},
            "counts": {
                "consoleMessages": len(signals.console_messages),
                "failedRequests": len(signals.failed_requests),
                "ignoredConsoleErrors": len(collect_ignored_console_errors(signals, ignored_console_patterns)),
                "pageErrors": len(signals.page_errors),
                "requests": len(signals.requests),
                "responseBodies": len(signals.response_bodies),
                "responses": len(signals.responses),
            },
        },
    )
    (output_dir / "console.txt").write_text(f"{chr(10).join(signals.console_messages)}\n", encoding="utf-8")
    write_json(output_dir / "console_details.json", signals.console_details)
    write_json(output_dir / "failed_requests.json", signals.failed_requests)
    write_json(
        output_dir / "ignored_console_errors.json", collect_ignored_console_errors(signals, ignored_console_patterns)
    )
    write_json(
        output_dir / "network.json",
        {
            "requestFailures": signals.request_failures,
            "requestFinishes": signals.request_finishes,
            "requests": signals.requests,
            "responses": signals.responses,
        },
    )
    write_json(output_dir / "page_state.json", page_state)
    (output_dir / "page.html").write_text(page_state.get("html", ""), encoding="utf-8")
    try:
        await page.screenshot(path=str(output_dir / "page.png"), full_page=True)
    except Exception as screenshot_error:
        (output_dir / "screenshot_error.txt").write_text(f"{screenshot_error}\n", encoding="utf-8")
    return output_dir


def serialize_error(error: BaseException) -> dict[str, str]:
    return {"message": str(error), "name": error.__class__.__name__}


def write_json(path: Path, value: Any) -> None:
    path.write_text(f"{json.dumps(value, indent=2)}\n", encoding="utf-8")


def chromium_executable() -> str | None:
    root = os.environ.get("CHROMIUM_HEADLESS_SHELL") or os.environ.get("PUPPETEER_EXECUTABLE_PATH")
    if not root:
        return None
    path = Path(root)
    candidate = path / "chrome-linux" / "headless_shell"
    return str(candidate if candidate.exists() else path)


def format_json(value: Any) -> str:
    return json.dumps(value, indent=2)
