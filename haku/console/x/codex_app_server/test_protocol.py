import json
from pathlib import Path

import pytest
import pytest_bazel

from haku.console.x.codex_app_server.protocol import (
    Direction,
    Notification,
    Request,
    Response,
    UnknownMessage,
    parse_message,
    read_trace,
)


def test_envelopes_parse_without_requiring_a_jsonrpc_member():
    assert isinstance(parse_message({"method": "initialize", "id": 1, "params": {}}), Request)
    assert isinstance(parse_message({"method": "turn/completed", "params": {}}), Notification)
    assert isinstance(parse_message({"id": 1, "result": {}}), Response)


def test_unknown_or_future_envelopes_are_values_not_exceptions():
    malformed = parse_message({"method": "future/method", "params": []})
    assert malformed == UnknownMessage(reason="future/method/params", raw={"method": "future/method", "params": []})


def test_trace_reader_reports_the_bad_source_line(tmp_path: Path):
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps({"seq": 1, "direction": Direction.SERVER_TO_CLIENT, "message": {}}) + "\n[]\n")
    with pytest.raises(ValueError, match=r"bad\.jsonl:2"):
        read_trace(path)


def test_trace_reader_rejects_a_non_string_direction(tmp_path: Path):
    path = tmp_path / "bad-direction.jsonl"
    path.write_text(json.dumps({"seq": 1, "direction": 1, "message": {}}) + "\n")
    with pytest.raises(ValueError, match=r"bad-direction\.jsonl:1: malformed trace record"):
        read_trace(path)


def test_trace_reader_rejects_non_monotonic_sequences(tmp_path: Path):
    path = tmp_path / "bad-sequence.jsonl"
    path.write_text(
        json.dumps({"seq": 2, "direction": "server_to_client", "message": {}})
        + "\n"
        + json.dumps({"seq": 2, "direction": "server_to_client", "message": {}})
        + "\n"
    )
    with pytest.raises(ValueError, match=r"bad-sequence\.jsonl:2: malformed trace record"):
        read_trace(path)


def test_trace_reader_accepts_reviewed_payload_wrapper(tmp_path: Path):
    path = tmp_path / "staged.jsonl"
    path.write_text(
        json.dumps(
            {
                "direction": "server_to_client",
                "payload": {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
            }
        )
        + "\n"
    )
    records = read_trace(path)
    assert records[0].seq == 1
    assert records[0].direction is Direction.SERVER_TO_CLIENT
    assert records[0].message["method"] == "turn/completed"


if __name__ == "__main__":
    pytest_bazel.main()
