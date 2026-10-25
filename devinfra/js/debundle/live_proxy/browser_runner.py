from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from re import Pattern
from typing import TYPE_CHECKING, Any, cast

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from util.bazel.runfiles import get_required_path
from util.testing.undeclared_outputs import undeclared_outputs_dir

if TYPE_CHECKING:
    from playwright.async_api import Browser, Page, Playwright

    from devinfra.js.debundle.live_proxy.core import LiveProxyConfig


@dataclass
class BrowserSignals:
    console_details: list[dict[str, Any]] = field(default_factory=list)
    console_messages: list[str] = field(default_factory=list)
    failed_requests: list[dict[str, Any]] = field(default_factory=list)
    page_errors: list[str] = field(default_factory=list)
    request_failures: list[dict[str, Any]] = field(default_factory=list)
    request_finishes: list[dict[str, Any]] = field(default_factory=list)
    requests: list[dict[str, Any]] = field(default_factory=list)
    responses: list[dict[str, Any]] = field(default_factory=list)
    pending_tasks: set[asyncio.Task[None]] = field(default_factory=set)


async def launch_chromium_for_proxy(playwright: Playwright, config: LiveProxyConfig) -> Browser:
    return await playwright.chromium.launch(
        executable_path=chromium_executable(),
        headless=True,
        args=[
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-setuid-sandbox",
            "--no-sandbox",
            f"--ignore-certificate-errors-spki-list={certificate_spki(config.ca_dir / 'mitmproxy-ca-cert.pem')}",
            f"--proxy-server=http://{config.proxy_host}:{config.proxy_port}",
        ],
    )


def certificate_spki(path: Path) -> str:
    certificate = x509.load_pem_x509_certificate(path.read_bytes())
    public_key = certificate.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return base64.b64encode(hashlib.sha256(public_key).digest()).decode("ascii")


def chromium_executable() -> str:
    executable = os.environ.get("CHROMIUM_HEADLESS_SHELL")
    if not executable:
        raise RuntimeError("CHROMIUM_HEADLESS_SHELL must name the pinned Chromium executable in runfiles")
    return str(get_required_path(executable))


def collect_browser_signals(page: Page) -> BrowserSignals:
    signals = BrowserSignals()
    request_ids: dict[Any, int] = {}
    next_request_id = 1
    next_response_id = 1

    async def describe_console_message(message) -> None:
        args = []
        for arg in message.args:
            try:
                args.append(
                    await arg.evaluate("value => JSON.parse(JSON.stringify(value, Object.getOwnPropertyNames(value)))")
                )
            except Exception as error:
                args.append({"unserializable": str(error)})
        signals.console_details.append({"args": args, "text": message.text, "type": message.type})

    def on_console(message) -> None:
        signals.console_messages.append(f"[{message.type}] {message.text}")
        track_task(signals, describe_console_message(message))

    def on_request(request) -> None:
        nonlocal next_request_id
        request_ids[request] = next_request_id
        signals.requests.append(
            {
                "id": next_request_id,
                "isNavigationRequest": request.is_navigation_request(),
                "method": request.method,
                "resourceType": request.resource_type,
                "url": request.url,
            }
        )
        next_request_id += 1

    def on_request_failed(request) -> None:
        record = {"errorText": request.failure or "unknown", "requestId": request_ids.get(request), "url": request.url}
        signals.failed_requests.append(record)
        signals.request_failures.append(record)

    def on_request_finished(request) -> None:
        signals.request_finishes.append({"requestId": request_ids.get(request), "url": request.url})

    def on_response(response) -> None:
        nonlocal next_response_id
        request = response.request
        signals.responses.append(
            {
                "fromServiceWorker": response.from_service_worker,
                "id": next_response_id,
                "requestId": request_ids.get(request),
                "resourceType": request.resource_type,
                "status": response.status,
                "statusText": response.status_text,
                "url": response.url,
            }
        )
        next_response_id += 1

    page.on("console", on_console)
    page.on("pageerror", lambda error: signals.page_errors.append(str(error)))
    page.on("request", on_request)
    page.on("requestfailed", on_request_failed)
    page.on("requestfinished", on_request_finished)
    page.on("response", on_response)
    return signals


def track_task(signals: BrowserSignals, coroutine: Coroutine[Any, Any, None]) -> None:
    task: asyncio.Task[None] = asyncio.create_task(coroutine)
    signals.pending_tasks.add(task)
    task.add_done_callback(signals.pending_tasks.discard)


def collect_console_errors(signals: BrowserSignals, ignored_patterns: Iterable[str | Pattern[str]] = ()) -> list[str]:
    ignored = compile_patterns(ignored_patterns)
    return [
        message
        for message in signals.console_messages
        if message.startswith("[error] ") and not any(pattern.search(message) for pattern in ignored)
    ]


def collect_ignored_console_errors(
    signals: BrowserSignals, ignored_patterns: Iterable[str | Pattern[str]] = ()
) -> list[str]:
    ignored = compile_patterns(ignored_patterns)
    return [
        message
        for message in signals.console_messages
        if message.startswith("[error] ") and any(pattern.search(message) for pattern in ignored)
    ]


def compile_patterns(patterns: Iterable[str | Pattern[str]]) -> list[Pattern[str]]:
    return [pattern if isinstance(pattern, re.Pattern) else re.compile(pattern) for pattern in patterns]


def assert_no_console_errors(
    signals: BrowserSignals, detail: str, ignored_patterns: Iterable[str | Pattern[str]] = ()
) -> None:
    console_errors = collect_console_errors(signals, ignored_patterns)
    assert console_errors == [], f"console errors:\n{chr(10).join(console_errors)}\n{detail}"


async def settle_pending_tasks(signals: BrowserSignals, timeout_s: float = 1.0) -> None:
    if signals.pending_tasks:
        await asyncio.wait(signals.pending_tasks, timeout=timeout_s)


async def collect_failure_diagnostics(
    page: Page, signals: BrowserSignals, read_page_state: Callable[[Page], Awaitable[dict[str, Any]]] | None = None
) -> str:
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


async def safe_read_page_state(
    page: Page, read_page_state: Callable[[Page], Awaitable[dict[str, Any]]] | None = None
) -> dict[str, Any]:
    try:
        if read_page_state is not None:
            return await read_page_state(page)
        base = cast(
            dict[str, Any],
            await page.evaluate(
                """() => ({
                    bodyText: document.body?.innerText ?? "",
                    documentReadyState: document.readyState,
                    html: document.documentElement?.outerHTML ?? "",
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
    ignored_console_patterns: Iterable[str | Pattern[str]] = (),
    read_page_state: Callable[[Page], Awaitable[dict[str, Any]]] | None = None,
) -> Path | None:
    if not os.environ.get("TEST_UNDECLARED_OUTPUTS_DIR"):
        return None
    output_dir = undeclared_outputs_dir()
    await settle_pending_tasks(signals, timeout_s=5.0)
    page_state = await safe_read_page_state(page, read_page_state)
    ignored = tuple(ignored_console_patterns)
    await asyncio.to_thread(_write_diagnostic_files, output_dir, signals, error, page_state, ignored)
    try:
        await page.screenshot(path=str(output_dir / "page.png"), full_page=True)
    except Exception as screenshot_error:
        await asyncio.to_thread(_write_text, output_dir / "screenshot_error.txt", f"{screenshot_error}\n")
    return output_dir


def _write_diagnostic_files(
    output_dir: Path,
    signals: BrowserSignals,
    error: BaseException | None,
    page_state: dict[str, Any],
    ignored_console_patterns: Iterable[str | Pattern[str]],
) -> None:
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


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def serialize_error(error: BaseException) -> dict[str, str]:
    return {"message": str(error), "name": error.__class__.__name__}


def write_json(path: Path, value: Any) -> None:
    path.write_text(f"{json.dumps(value, indent=2)}\n", encoding="utf-8")


def format_json(value: Any) -> str:
    return json.dumps(value, indent=2)
