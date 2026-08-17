"""The properties a reader of the frame log may rely on across a compaction.

The fixture is one real session captured by `probes/compaction.py` and scrubbed by
`probes/redact_capture.py` (claude-code 2.1.233). Each assertion is a claim <protocol.md> makes
about compaction, hooks, or client-hosted tools, held against the one run in which those things
happened. A CLI repin that breaks one has broken something a consumer of this protocol depends on.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_bazel

from util.bazel.runfiles import get_required_path

CAPTURE = get_required_path("ducktape/haku/cli_protocol/testdata/compaction_session.jsonl")


@pytest.fixture(scope="module")
def records() -> list[dict[str, Any]]:
    return [json.loads(line) for line in CAPTURE.read_text(encoding="utf-8").splitlines()]


@pytest.fixture(scope="module")
def inbound(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record["frame"] for record in records if record["direction"] == "in" and "frame" in record]


@pytest.fixture(scope="module")
def outbound(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [record["frame"] for record in records if record["direction"] == "out" and "frame" in record]


@pytest.fixture(scope="module")
def boundary_index(inbound: list[dict[str, Any]]) -> int:
    return next(i for i, frame in enumerate(inbound) if frame.get("subtype") == "compact_boundary")


def test_the_conversation_that_was_compacted_is_still_in_the_log(
    inbound: list[dict[str, Any]], boundary_index: int
) -> None:
    """Nothing is retracted. A compaction appends; it never edits or drops an earlier frame.

    That makes a stored cursor safe *as a record* — replaying any prefix yields the frames it
    yielded before — but not as the conversation; see the two tests below.
    """
    before = inbound[:boundary_index]

    assert [frame for frame in before if frame.get("type") == "assistant"]
    assert [frame for frame in before if frame.get("type") == "result"]
    assert len({frame["uuid"] for frame in inbound if "uuid" in frame}) == len(
        [frame for frame in inbound if "uuid" in frame]
    )


def test_the_boundary_carries_accounting_and_no_replacement_text(
    inbound: list[dict[str, Any]], boundary_index: int
) -> None:
    """`compact_boundary` is a marker. It says a compaction happened and how much it dropped."""
    metadata = inbound[boundary_index]["compact_metadata"]

    assert metadata["trigger"] == "auto"
    assert metadata["pre_tokens"] > metadata["post_tokens"]
    assert metadata["cumulative_dropped_tokens"] == metadata["pre_tokens"] - metadata["post_tokens"]
    assert "message" not in inbound[boundary_index]


def test_the_replacement_transcript_arrives_as_a_synthetic_user_frame(
    inbound: list[dict[str, Any]], boundary_index: int
) -> None:
    """The frame that rewrites the conversation is the *next* one, and it looks like a prompt.

    A reader that renders `user` frames and skips subtypes it does not know will render this as
    something the user said. `isSynthetic` is the only thing distinguishing it, and it is not in
    the CLI's own documented field set.
    """
    summary = inbound[boundary_index + 1]

    assert summary["type"] == "user"
    assert summary["isSynthetic"] is True
    assert [block["type"] for block in summary["message"]["content"]] == ["text"]
    assert "ran out of context" in summary["message"]["content"][0]["text"]


def test_the_client_never_sent_the_summary(outbound: list[dict[str, Any]], inbound: list[dict[str, Any]]) -> None:
    """The one user turn in the transcript that no prompt of ours accounts for."""
    prompts = {
        frame["message"]["content"]
        for frame in outbound
        if frame.get("type") == "user" and isinstance(frame["message"]["content"], str)
    }
    summary = next(frame for frame in inbound if frame.get("isSynthetic"))

    assert summary["message"]["content"][0]["text"] not in prompts


def test_the_boundarys_backpointer_does_not_resolve_in_the_frame_log(
    records: list[dict[str, Any]], inbound: list[dict[str, Any]], boundary_index: int
) -> None:
    """`logical_parent_uuid` points into the CLI's on-disk transcript, not into the stream.

    It reads like the relink a consumer wants — "the last message still in context" — but the uuid
    it names occurs exactly once in the whole capture, in this frame, and belongs to no frame the
    CLI ever sent: it is one of the compaction's own internal turns, which the wire never carries.

    So the cut a reader can act on is **positional**: everything before this frame's index is out of
    the model's context, which is what a stored cursor already records.
    """
    parent = inbound[boundary_index]["logical_parent_uuid"]

    assert parent not in {frame.get("uuid") for record in records if (frame := record.get("frame"))}


def test_a_partial_compaction_would_carry_a_preserved_segment(
    inbound: list[dict[str, Any]], boundary_index: int
) -> None:
    """This run summarised everything, so the partial case is unobserved and stays that way.

    The CLI's schema documents `preserved_segment` (`anchor_uuid` plus the kept uuids) for a
    compaction that keeps a suffix. Its absence here is what says the whole prefix went — a reader
    that only implements `logical_parent_uuid` is correct for this shape and wrong for that one.
    """
    assert "preserved_segment" not in inbound[boundary_index]["compact_metadata"]


def test_the_precompact_hook_runs_before_the_boundary(inbound: list[dict[str, Any]], boundary_index: int) -> None:
    """A client registering `PreCompact` is asked, and asked in time to matter."""
    hooks = [
        (i, frame["request"])
        for i, frame in enumerate(inbound)
        if frame.get("type") == "control_request" and (frame["request"]).get("callback_id") == "cb_PreCompact"
    ]

    assert len(hooks) == 1
    index, request = hooks[0]
    assert index < boundary_index
    assert request["input"]["trigger"] == "auto"


def test_session_start_fires_on_compaction_and_not_at_startup(inbound: list[dict[str, Any]]) -> None:
    """The only `SessionStart` this session ever sees is the compaction's.

    <protocol.md> already says the event does not fire for a session the client initializes over
    `initialize`. The sharpening is that it fires anyway on a compaction, carrying `source:
    "compact"` — so a client that treats `SessionStart` as "the session began" will re-run its
    startup work mid-session, at the one moment the context was just thrown away.
    """
    sources = [
        frame["request"]["input"]["source"]
        for frame in inbound
        if frame.get("type") == "control_request" and (frame["request"]).get("callback_id") == "cb_SessionStart"
    ]

    assert sources == ["compact"]


def test_every_inbound_control_request_was_answered(
    inbound: list[dict[str, Any]], outbound: list[dict[str, Any]]
) -> None:
    """Silence on one request stalls the turn indefinitely, so the capture must show none."""
    asked = {frame["request_id"] for frame in inbound if frame.get("type") == "control_request"}
    answered = {frame["response"]["request_id"] for frame in outbound if frame.get("type") == "control_response"}

    assert asked == answered


def test_the_client_hosted_tool_reached_the_model(inbound: list[dict[str, Any]]) -> None:
    """`sdkMcpServers` end to end: served here as JSON-RPC, called by the model as an MCP tool."""
    served = {
        frame["request"]["message"]["method"]
        for frame in inbound
        if frame.get("type") == "control_request" and (frame["request"]).get("subtype") == "mcp_message"
    }
    called = {
        block["name"]
        for frame in inbound
        if frame.get("type") == "assistant"
        for block in frame["message"]["content"]
        if block["type"] == "tool_use"
    }

    assert {"initialize", "tools/list", "tools/call"} <= served
    assert "mcp__probe__haku_filler" in called


def test_the_capture_preserves_stdout_the_cli_writes_between_frames(records: list[dict[str, Any]]) -> None:
    """Not every stdout line is a frame, and a reader that assumes otherwise raises on prose.

    This run produced none, so the assertion is that the capture format keeps the distinction
    rather than that a line occurred: a record is a frame or a `stdout_line`, never neither.
    """
    assert records
    assert all(("frame" in record) ^ ("stdout_line" in record) for record in records)


if __name__ == "__main__":
    pytest_bazel.main()
