import asyncio
import json
import os
import stat
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_bazel
from mitmproxy import connection, ctx, exceptions, http
from mitmproxy.websocket import WebSocketData, WebSocketMessage

from devinfra.github_api_capture.session_ws_metadata import (
    MAX_JSON_BYTES,
    OPTION_NAMES,
    ROUTE,
    SessionWebSocketMetadata,
    summarize,
)


def make_flow(url: str = "https://claude.ai/v1/sessions/ws/test-private-session/subscribe", method: str = "GET"):
    flow = http.HTTPFlow(
        connection.Client(peername=("127.0.0.1", 12345), sockname=("127.0.0.1", 12346)),
        connection.Server(address=("claude.ai", 443)),
    )
    flow.request = http.Request.make(method, url, headers=http.Headers(authorization="test-private-token"))
    flow.websocket = WebSocketData()
    return flow


@pytest.fixture
async def recorder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SessionWebSocketMetadata]:
    monkeypatch.setattr(
        ctx,
        "options",
        SimpleNamespace(
            record_cloud_session_ws=True, cloud_session_ws_events=str(tmp_path / "private" / "events.jsonl")
        ),
        raising=False,
    )
    addon = SessionWebSocketMetadata()
    addon.configure(OPTION_NAMES)
    addon.running()
    try:
        yield addon
    finally:
        addon.done()
        assert addon.heartbeat is not None
        await asyncio.gather(addon.heartbeat, return_exceptions=True)


def rows(addon: SessionWebSocketMetadata):
    assert addon.output is not None
    return [json.loads(line) for line in addon.output.read_text().splitlines()]


def test_default_off_and_startup_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    options = SimpleNamespace(record_cloud_session_ws=False, cloud_session_ws_events=str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(ctx, "options", options, raising=False)
    addon = SessionWebSocketMetadata()
    addon.configure(OPTION_NAMES)
    addon.running()
    flow = make_flow()
    addon.websocket_start(flow)
    addon.websocket_end(flow)
    addon.done()
    assert addon.heartbeat is None
    assert addon.flows == {}
    assert not Path(options.cloud_session_ws_events).exists()
    with pytest.raises(exceptions.OptionsError, match="startup-only"):
        addon.configure(OPTION_NAMES)
    options.record_cloud_session_ws = True
    options.cloud_session_ws_events = ""
    with pytest.raises(exceptions.OptionsError, match="requires an output path"):
        SessionWebSocketMetadata().configure(OPTION_NAMES)


@pytest.mark.parametrize(
    ("method", "url", "matches"),
    [
        ("GET", "https://claude.ai/v1/sessions/ws/test-private-session/subscribe", True),
        ("GET", "https://claude.ai/v1/sessions/ws/test-private-session/subscribe?token=test-private", True),
        ("POST", "https://claude.ai/v1/sessions/ws/test-private-session/subscribe", False),
        ("GET", "https://other.test/v1/sessions/ws/test-private-session/subscribe", False),
        ("GET", "https://claude.ai.evil.test/v1/sessions/ws/test-private-session/subscribe", False),
        ("GET", "https://claude.ai/v1/sessions/ws/test-private-session/subscribe/extra", False),
        ("GET", "https://claude.ai/v1/sessions/ws/nested/test-private-session/subscribe", False),
        ("GET", "https://claude.ai/v1/sessions/ws//subscribe", False),
        ("GET", "https://claude.ai/v1/code/sessions/test-private-session/events", False),
    ],
)
async def test_exact_route(recorder: SessionWebSocketMetadata, method: str, url: str, matches: bool) -> None:
    flow = make_flow(url, method)
    before = flow.get_state()
    recorder.websocket_start(flow)
    recorder.websocket_end(flow)
    assert recorder.totals.flows_started == int(matches)
    assert recorder.totals.flows_ended == int(matches)
    assert flow.get_state() == before
    assert len(rows(recorder)) == (3 if matches else 1)
    assert recorder.output is not None
    assert "test-private" not in recorder.output.read_text()


@pytest.mark.parametrize(
    ("content", "opcode", "status"),
    [
        (b"\xff", 1, "non_json"),
        (b"test-private", 1, "non_json"),
        (b"test-private", 2, "binary"),
        (b"x" * (MAX_JSON_BYTES + 1), 1, "oversized"),
        (b"9" * 5000, 1, "analysis_limit"),
        (b"[]", 1, "unknown_schema"),
        (b'{"type":"test-private"}', 1, "unknown_schema"),
        (b'{"type":{},"test-private":"test-private"}', 1, "unknown_schema"),
        (b'{"type":"assistant","message":{"role":"user","content":[]}}', 1, "unknown_schema"),
    ],
    ids=[
        "invalid-utf8",
        "non-json",
        "binary",
        "oversized",
        "integer-limit",
        "list-root",
        "unknown-type",
        "dict-type",
        "wrong-role",
    ],
)
async def test_uninspected_is_explicit(
    recorder: SessionWebSocketMetadata, content: bytes, opcode: int, status: str
) -> None:
    flow = make_flow()
    recorder.websocket_start(flow)
    message = WebSocketMessage(opcode, False, content)
    assert flow.websocket is not None
    flow.websocket.messages.append(message)
    before = message.get_state()
    recorder.websocket_message(flow)
    row = rows(recorder)[-1]
    assert row["parse_status"] == status
    assert row["structure"] is None
    assert row["parse_totals"][status] == 1
    assert row["payload_bytes"] == len(content)
    assert row["direction"] == "server_to_client"
    assert message.get_state() == before


async def test_private_metadata_and_cumulative_heartbeat(recorder: SessionWebSocketMetadata) -> None:
    flow = make_flow()
    recorder.websocket_start(flow)
    payload = {
        "type": "assistant",
        "session_id": "test-private-session",
        "uuid": "test-private-user",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "name": "Bash", "id": "test-private-id", "input": {"command": "test-private"}},
                {"type": "tool_use", "name": "exec_command", "input": {"cmd": "test-private"}},
                {"type": "tool_use", "name": "test-private-name", "input": {"command": "test-private"}},
                {"type": "text", "text": "test-private-prompt"},
                {"type": "test-private-type", "test-private-key": "test-private-value"},
            ],
        },
    }
    assert flow.websocket is not None
    message = WebSocketMessage(1, False, json.dumps(payload).encode(), timestamp=1234)
    flow.websocket.messages.append(message)
    recorder.websocket_message(flow)
    flow.websocket.messages.append(WebSocketMessage(2, True, b"test-private-output"))
    recorder.websocket_message(flow)
    recorder.record("heartbeat")
    record = rows(recorder)[-3]
    assert record["message_at"] == 1234
    assert record["structure"]["tool_use_bash"] == 1
    assert record["structure"]["tool_use_exec"] == 1
    assert record["structure"]["tool_use_other"] == 1
    assert record["structure"]["unknown_blocks"] == 1
    heartbeat = rows(recorder)[-1]
    assert heartbeat["active_flows"] == 1
    assert heartbeat["totals"]["server_messages"] == 1
    assert heartbeat["totals"]["client_messages"] == 1
    assert heartbeat["parse_totals"]["recognized"] == heartbeat["parse_totals"]["binary"] == 1
    recorder.websocket_end(flow)
    all_rows = rows(recorder)
    assert all(row["route"] == ROUTE for row in all_rows)
    assert len({row["flow_id"] for row in all_rows if row["flow_id"] is not None}) == 1
    assert all_rows[-1]["active_flows"] == 0
    assert recorder.output is not None
    assert "test-private" not in recorder.output.read_text()
    assert flow.id not in recorder.output.read_text()
    assert stat.S_IMODE(recorder.output.stat().st_mode) == 0o600
    assert stat.S_IMODE(recorder.output.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize(
    ("value", "field"),
    [
        ({"type": "system"}, "system_messages"),
        ({"type": "result"}, "result_messages"),
        ({"type": "tool_progress", "tool_name": "test-private"}, "tool_progress"),
        ({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result"}]}}, "tool_results"),
        (
            {
                "type": "stream_event",
                "event": {"type": "content_block_start", "content_block": {"type": "tool_use", "name": "Bash"}},
            },
            "tool_use_bash",
        ),
        (
            {
                "type": "stream_event",
                "event": {
                    "type": "content_block_delta",
                    "delta": {"type": "input_json_delta", "partial_json": "test-private"},
                },
            },
            "input_json_deltas",
        ),
    ],
)
def test_structural_shapes(value: dict, field: str) -> None:
    status, structure = summarize(WebSocketMessage(1, False, json.dumps(value).encode()))
    assert status == "recognized"
    assert structure is not None
    assert vars(structure)[field] == 1


def test_parser_recursion_limit_is_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    def recursion_limit(text: str):
        raise RecursionError("test-private-payload")

    monkeypatch.setattr(json, "loads", recursion_limit)
    assert summarize(WebSocketMessage(1, False, b"{}")) == ("analysis_limit", None)


async def test_append_and_restrict_existing_file(recorder: SessionWebSocketMetadata) -> None:
    assert recorder.output is not None
    before = recorder.output.read_text()
    recorder.output.chmod(0o644)
    next_lifetime = SessionWebSocketMetadata()
    next_lifetime.output = recorder.output
    next_lifetime.record("started")
    assert recorder.output.read_text().startswith(before)
    assert len(rows(recorder)) == 2
    assert stat.S_IMODE(recorder.output.stat().st_mode) == 0o600


@pytest.mark.parametrize("kind", ["symlink", "fifo", "directory"])
async def test_write_failure_preserves_traffic_and_is_counted(
    recorder: SessionWebSocketMetadata, tmp_path: Path, caplog: pytest.LogCaptureFixture, kind: str
) -> None:
    target = tmp_path / "test-private-target"
    target.write_text("preserve")
    invalid = tmp_path / "test-private-invalid"
    if kind == "symlink":
        invalid.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(invalid)
    else:
        invalid.mkdir()
    output = recorder.output
    recorder.output = invalid
    flow = make_flow()
    before = flow.get_state()
    recorder.websocket_start(flow)
    assert recorder.totals.write_failures == 1
    assert flow.get_state() == before
    assert "test-private" not in caplog.text
    assert target.read_text() == "preserve"
    recorder.output = output
    recorder.record("heartbeat")
    assert rows(recorder)[-1]["totals"]["write_failures"] == 1


if __name__ == "__main__":
    pytest_bazel.main()
