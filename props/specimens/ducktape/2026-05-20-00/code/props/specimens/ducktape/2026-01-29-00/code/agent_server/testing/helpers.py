from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any

from uvicorn import Config, Server

from net_util.net import pick_free_port
from openai_utils.model import OpenAIModelProto, ResponsesRequest, ResponsesResult

# System notification tag constants
SYSTEM_NOTIFICATION_START_TAG = "<system notification>"
SYSTEM_NOTIFICATION_END_TAG = "</system notification>"


@dataclass
class ServerHandle:
    """Handle for a running uvicorn server with cleanup."""

    base_url: str
    app: Any
    _stop_fn: Any

    def stop(self) -> None:
        """Stop the server."""
        self._stop_fn()


class NoopOpenAIClient(OpenAIModelProto):
    """No-op OpenAI client for tests that bypass sampling via SyntheticAction."""

    def __init__(self) -> None:
        self.model = "noop-model"

    async def responses_create(self, req: ResponsesRequest) -> ResponsesResult:
        # This should never be called when using SyntheticAction path
        raise NotImplementedError("NoopOpenAIClient should not be called in SyntheticAction path")


def strip_system_notification_wrapper(text: str) -> str:
    """Strip system notification wrapper tags if present, returning inner content."""
    start_tag = SYSTEM_NOTIFICATION_START_TAG + "\n"
    end_tag = "\n" + SYSTEM_NOTIFICATION_END_TAG
    if text.startswith(start_tag) and text.endswith(end_tag):
        return text[len(start_tag) : -len(end_tag)]
    return text


def start_uvicorn_app(
    app: Any, *, host: str = "127.0.0.1", port: int | None = None, log_level: str = "info"
) -> ServerHandle:
    """Start a FastAPI app in a background thread with uvicorn.

    Returns a ServerHandle with base_url, app, and stop() method.
    Waits until app.state.ready is set if present (best-effort).
    """
    if port is None:
        port = pick_free_port(host)
    cfg = Config(app=app, host=host, port=port, log_level=log_level, loop="asyncio")
    server = Server(cfg)
    th = threading.Thread(target=server.run, name="uvicorn-server", daemon=True)
    th.start()
    started = time.time()
    # Wait until app startup has completed (ready Event set) or timeout
    while not getattr(app.state, "ready", None) or not app.state.ready.is_set():
        if time.time() - started > 10:
            raise RuntimeError("server failed to start within 10s")
        time.sleep(0.05)

    def _stop() -> None:
        server.should_exit = True
        th.join(timeout=10)

    return ServerHandle(base_url=f"http://{host}:{port}", app=app, _stop_fn=_stop)
