"""FastAPI app for the hook daemon.

Handles all Claude Code hook types over UDS. Imports expensive modules once at
startup (pydantic, opentelemetry, session_start) so individual hook calls are fast.

The auth proxy runs in-process as daemon threads, started at server startup.
Session start writes credentials; the proxy reads them on each connection.
"""

import asyncio
import errno
import json
import logging
import signal
import time
from pathlib import Path

from fastapi import FastAPI
from opentelemetry import trace

from devinfra.claude.auth_proxy.proxy import AuthForwardingProxy
from devinfra.claude.auth_proxy.vars import get_upstream_proxy_url
from devinfra.claude.claude_api.hooks.dispatch_output import AnyHookOutput
from devinfra.claude.claude_api.hooks.post_tool_use import PostToolUseInput
from devinfra.claude.claude_api.hooks.pre_tool_use import PreToolUseInput
from devinfra.claude.claude_api.hooks.session_start import SessionStartHookInput
from devinfra.claude.hook_daemon.models import HookRequest, HookResponse
from devinfra.claude.hook_daemon.post_tool_use import evaluate as evaluate_post
from devinfra.claude.hook_daemon.pre_tool_use import evaluate as evaluate_pre
from devinfra.claude.hook_daemon.session_start.handler import handle as handle_session_start
from devinfra.claude.hook_daemon.session_start.http_client import build_http_client
from devinfra.claude.hook_daemon.tracing import DeferredOtlpExporter
from devinfra.claude.session_paths import SessionPaths
from devinfra.claude.settings import HookSettings

logger = logging.getLogger(__name__)

IDLE_TIMEOUT_SECONDS = 1800  # 30 minutes
IDLE_CHECK_INTERVAL_SECONDS = 30

app = FastAPI()


def configure(daemon_dir: Path, otlp_exporter: DeferredOtlpExporter) -> None:
    """Set daemon runtime directory and shared config. Call before starting uvicorn."""
    app.state.daemon_dir = daemon_dir
    app.state.settings = HookSettings()
    app.state.otlp_exporter = otlp_exporter
    app.state.last_request_time = time.monotonic()
    app.state.proxy = None
    app.state.background_tasks = set[asyncio.Task[object]]()

    # Start auth proxy in-process if upstream proxy is configured.
    # The proxy binds the port immediately; credentials are written later
    # by session_start (proxy reads creds file on each connection).
    #
    # Port conflict (EADDRINUSE): Claude Code may send Setup and SessionStart hooks
    # with *different* session IDs (e.g. Setup for the new session after compaction,
    # SessionStart for the old/compacting session). This causes two daemon instances
    # to start concurrently, both trying to bind the auth proxy port. The second
    # daemon logs a warning and skips starting its own proxy — the first daemon's
    # proxy is already serving on that port.
    if get_upstream_proxy_url():
        settings: HookSettings = app.state.settings
        creds_file = daemon_dir / "upstream_proxy"
        proxy = AuthForwardingProxy(listen_port=settings.auth_proxy_port, creds_file=creds_file)
        try:
            proxy.start()
            app.state.proxy = proxy
            logger.info("Auth proxy started in-process on port %d", settings.auth_proxy_port)
        except OSError as e:
            if e.errno == errno.EADDRINUSE:
                logger.warning(
                    "Auth proxy port %d already in use — another daemon instance has it. "
                    "Skipping proxy start for this daemon.",
                    settings.auth_proxy_port,
                )
                # Running without a proxy would leave app.state.proxy as None, but
                # SessionStart handling asserts that a proxy is available. Fail fast
                # instead of starting a partially-broken daemon.
                raise RuntimeError(
                    f"Auth proxy port {settings.auth_proxy_port} already in use; "
                    "this daemon cannot start its auth proxy and will exit."
                ) from e
            raise


def _save_session_env(env: dict[str, str]) -> None:
    """Persist caller's env to disk for debuggability and daemon restart survival."""
    daemon_dir: Path | None = getattr(app.state, "daemon_dir", None)
    if daemon_dir is None:
        return
    env_file = daemon_dir / "session_env.json"
    env_file.write_text(json.dumps(env, indent=2))


@app.post("/hook")
async def handle_hook(req: HookRequest) -> HookResponse:
    app.state.last_request_time = time.monotonic()

    tracer = trace.get_tracer(__name__)
    hook_name = req.hook.hook_event_name

    with tracer.start_as_current_span(
        f"hook.{hook_name}",
        attributes={
            "hook.event_name": hook_name,
            "hook.session_id": req.hook.session_id,
            "hook.input": req.model_dump_json(),
        },
    ) as span:
        # Persist env to disk on every call
        _save_session_env(req.env)

        output: AnyHookOutput | None = None
        match req.hook:
            case SessionStartHookInput():
                paths = SessionPaths.from_env(req.hook.session_id, req.env)
                with build_http_client(req.env) as http:
                    output = await handle_session_start(
                        req.hook,
                        paths,
                        app.state.settings,
                        caller_env=req.env,
                        http=http,
                        otlp_exporter=app.state.otlp_exporter,
                        proxy=app.state.proxy,
                        background_tasks=app.state.background_tasks,
                    )
            case PreToolUseInput():
                output = evaluate_pre(req.hook)
            case PostToolUseInput():
                output = evaluate_post(req.hook)
            case _:
                pass  # All other hooks: noop

        resp = HookResponse(output=output)
        resp_json = resp.model_dump_json(by_alias=True, exclude_none=True)

        span.set_attribute("hook.output", resp_json)
        logger.info("hook %s → %s", hook_name, resp_json)

        return resp


@app.get("/health")
async def health() -> dict[str, str]:
    """Health check — does NOT reset idle timer."""
    return {"status": "ok"}


async def _idle_watchdog() -> None:
    """Background task: exit after IDLE_TIMEOUT_SECONDS of no requests."""
    while True:
        await asyncio.sleep(IDLE_CHECK_INTERVAL_SECONDS)
        idle_seconds = time.monotonic() - app.state.last_request_time
        if idle_seconds >= IDLE_TIMEOUT_SECONDS:
            logger.info("Idle timeout reached (%.0fs), shutting down", idle_seconds)
            signal.raise_signal(signal.SIGTERM)
            return


@app.on_event("startup")
async def _start_idle_watchdog() -> None:
    app.state.watchdog_task = asyncio.create_task(_idle_watchdog())


@app.on_event("shutdown")
async def _stop_proxy() -> None:
    """Stop the in-process auth proxy on daemon shutdown."""
    proxy: AuthForwardingProxy | None = getattr(app.state, "proxy", None)
    if proxy is not None:
        logger.info("Stopping in-process auth proxy...")
        proxy.stop()
