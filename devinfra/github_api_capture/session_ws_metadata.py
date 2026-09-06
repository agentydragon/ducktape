"""Opt-in, incremental metadata for Claude session WebSockets; never retain payloads."""

import asyncio
import errno
import json
import logging
import os
import re
import stat
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from mitmproxy import ctx, exceptions, http
from mitmproxy.addonmanager import Loader
from mitmproxy.websocket import WebSocketMessage

ROUTE = "/v1/sessions/ws/:session/subscribe"
ROUTE_PATTERN = re.compile(r"/v1/sessions/ws/[^/?#]+/subscribe")
MAX_JSON_BYTES = 64 * 1024
HEARTBEAT_SECONDS = 30
OPTION_NAMES = {"record_cloud_session_ws", "cloud_session_ws_events"}
logger = logging.getLogger(__name__)

ParseStatus = Literal["recognized", "unknown_schema", "non_json", "binary", "oversized", "analysis_limit"]
Event = Literal["started", "websocket_start", "websocket_message", "websocket_end", "heartbeat", "stopped"]


@dataclass
class Structure:
    assistant_messages: int = 0
    user_messages: int = 0
    system_messages: int = 0
    result_messages: int = 0
    tool_progress: int = 0
    stream_events: int = 0
    tool_use_bash: int = 0
    tool_use_exec: int = 0
    tool_use_other: int = 0
    tool_results: int = 0
    input_json_deltas: int = 0
    unknown_blocks: int = 0

    def tool_use(self, name: object) -> None:
        if name in ("Bash", "bash"):
            self.tool_use_bash += 1
        elif name in ("exec_command", "functions.exec_command", "shell", "run_shell_command"):
            self.tool_use_exec += 1
        else:
            self.tool_use_other += 1


def summarize(message: WebSocketMessage) -> tuple[ParseStatus, Structure | None]:
    """Count structural occurrences, not unique calls, execution, or GitHub requests."""
    if not message.is_text:
        return "binary", None
    if len(message.content) > MAX_JSON_BYTES:
        return "oversized", None
    try:
        value = json.loads(message.content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "non_json", None
    except (ValueError, RecursionError):
        return "analysis_limit", None
    if not isinstance(value, dict):
        return "unknown_schema", None

    counts = Structure()
    match value.get("type"):
        case "assistant" | "user" as kind:
            envelope = value.get("message")
            if not isinstance(envelope, dict) or envelope.get("role") != kind:
                return "unknown_schema", None
            content = envelope.get("content")
            if not isinstance(content, list):
                return "unknown_schema", None
            counts.assistant_messages = int(kind == "assistant")
            counts.user_messages = int(kind == "user")
            for block in content:
                if not isinstance(block, dict):
                    counts.unknown_blocks += 1
                elif block.get("type") == "tool_use" and kind == "assistant":
                    counts.tool_use(block.get("name"))
                elif block.get("type") == "tool_result" and kind == "user":
                    counts.tool_results += 1
                elif block.get("type") not in ("text", "thinking", "redacted_thinking", "image"):
                    counts.unknown_blocks += 1
        case "system":
            counts.system_messages = 1
        case "result":
            counts.result_messages = 1
        case "tool_progress":
            counts.tool_progress = 1
        case "stream_event":
            event = value.get("event")
            if not isinstance(event, dict):
                return "unknown_schema", None
            counts.stream_events = 1
            match event.get("type"):
                case "content_block_start":
                    block = event.get("content_block")
                    if not isinstance(block, dict):
                        counts.unknown_blocks += 1
                    elif block.get("type") == "tool_use":
                        counts.tool_use(block.get("name"))
                    elif block.get("type") not in ("text", "thinking", "redacted_thinking"):
                        counts.unknown_blocks += 1
                case "content_block_delta":
                    delta = event.get("delta")
                    if not isinstance(delta, dict):
                        counts.unknown_blocks += 1
                    elif delta.get("type") == "input_json_delta":
                        counts.input_json_deltas = 1
                    elif delta.get("type") not in ("text_delta", "thinking_delta", "signature_delta"):
                        counts.unknown_blocks += 1
                case "message_start" | "message_delta" | "message_stop" | "content_block_stop":
                    pass
                case _:
                    return "unknown_schema", None
        case _:
            return "unknown_schema", None
    return "recognized", counts


@dataclass
class Totals:
    flows_started: int = 0
    flows_ended: int = 0
    client_messages: int = 0
    server_messages: int = 0
    payload_bytes: int = 0
    write_failures: int = 0


class SessionWebSocketMetadata:
    def __init__(self) -> None:
        self.output: Path | None = None
        self.started_at = time.time()
        self.flows: dict[str, str] = {}
        self.totals = Totals()
        self.parse_totals: dict[ParseStatus, int] = dict.fromkeys(
            ("recognized", "unknown_schema", "non_json", "binary", "oversized", "analysis_limit"), 0
        )
        self.heartbeat: asyncio.Task[None] | None = None
        self.is_running = False

    def load(self, loader: Loader) -> None:
        loader.add_option("record_cloud_session_ws", bool, False, "Opt in to private session WebSocket metadata.")
        loader.add_option("cloud_session_ws_events", str, "", "Private append-only JSONL metadata path.")

    def configure(self, updated: set[str]) -> None:
        if self.is_running and updated & OPTION_NAMES:
            raise exceptions.OptionsError("Session WebSocket metadata options are startup-only.")
        if ctx.options.record_cloud_session_ws and not ctx.options.cloud_session_ws_events:
            raise exceptions.OptionsError("Session WebSocket metadata requires an output path.")

    def running(self) -> None:
        self.is_running = True
        if not ctx.options.record_cloud_session_ws:
            return
        self.output = Path(ctx.options.cloud_session_ws_events)
        self.record("started")
        self.heartbeat = asyncio.create_task(self.heartbeats())

    async def heartbeats(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_SECONDS)
            self.record("heartbeat")

    def record(self, event: Event, *, flow_id: str | None = None, message: WebSocketMessage | None = None) -> None:
        if self.output is None:
            return
        details: dict[str, object] = {}
        if message is not None:
            status, structure = summarize(message)
            self.parse_totals[status] += 1
            self.totals.payload_bytes += len(message.content)
            if message.from_client:
                self.totals.client_messages += 1
            else:
                self.totals.server_messages += 1
            details = {
                "message_at": message.timestamp,
                "direction": "client_to_server" if message.from_client else "server_to_client",
                "payload_bytes": len(message.content),
                "frame_type": "text" if message.is_text else "binary",
                "parse_status": status,
                "structure": asdict(structure) if structure is not None else None,
            }
        record = {
            "event": event,
            "at": time.time(),
            "started_at": self.started_at,
            "flow_id": flow_id,
            "route": ROUTE,
            "active_flows": len(self.flows),
            "totals": asdict(self.totals),
            "parse_totals": self.parse_totals,
            **details,
        }
        try:
            self.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.output, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as output:
                if not stat.S_ISREG(os.fstat(output.fileno()).st_mode):
                    raise OSError(errno.EINVAL, "Metadata output must be a regular file")
                os.fchmod(output.fileno(), 0o600)
                output.write(json.dumps(record, separators=(",", ":")) + "\n")
        except OSError as error:
            self.totals.write_failures += 1
            # Paths and exception text can contain private identifiers. Keep traffic untouched.
            logger.error("Session WebSocket metadata write failed (errno=%s)", error.errno)  # noqa: TRY400 -- exception text is private

    def websocket_start(self, flow: http.HTTPFlow) -> None:
        if (
            self.output is not None
            and flow.request.method == "GET"
            and flow.request.host == "claude.ai"
            and ROUTE_PATTERN.fullmatch(flow.request.path.split("?", 1)[0])
        ):
            local_id = str(uuid.uuid4())
            self.flows[flow.id] = local_id
            self.totals.flows_started += 1
            self.record("websocket_start", flow_id=local_id)

    def websocket_message(self, flow: http.HTTPFlow) -> None:
        if (local_id := self.flows.get(flow.id)) is not None and flow.websocket is not None:
            self.record("websocket_message", flow_id=local_id, message=flow.websocket.messages[-1])

    def websocket_end(self, flow: http.HTTPFlow) -> None:
        if (local_id := self.flows.pop(flow.id, None)) is not None:
            self.totals.flows_ended += 1
            self.record("websocket_end", flow_id=local_id)

    def done(self) -> None:
        if self.heartbeat is not None:
            self.heartbeat.cancel()
        self.record("stopped")


addons = [SessionWebSocketMetadata()]
