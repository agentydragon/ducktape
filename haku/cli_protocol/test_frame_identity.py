"""Which frames a replay can recognise, and which must never be given an identity."""

from __future__ import annotations

import pytest_bazel

from haku.cli_protocol.frame_identity import frame_uid


def test_an_assistant_message_is_its_own_id() -> None:
    frame = {"type": "assistant", "message": {"id": "msg_01abc", "content": [{"type": "text", "text": "hi"}]}}

    assert frame_uid("assistant", frame) == "assistant:msg_01abc"


def test_a_tool_result_is_the_call_it_answers() -> None:
    """A `user` frame carrying results has no id of its own, so the call being answered is the
    only agent-assigned thing on it — and it is also what pairs the two in the rollout."""
    frame = {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_01xyz"}]},
    }

    assert frame_uid("user", frame) == "tool_result:toolu_01xyz"


def test_a_command_is_identified_by_its_state_as_well() -> None:
    """One command reports several states and each is its own frame, so the uuid alone would make
    `queued` and `started` collide — and collapsing those is exactly what stage 4 must not do."""
    queued = {"type": "command_lifecycle", "command_uuid": "cmd-1", "state": "queued"}
    started = {**queued, "state": "started"}

    assert frame_uid("command_lifecycle", queued) != frame_uid("command_lifecycle", started)


def test_a_delta_has_no_identity_and_must_not_be_given_one() -> None:
    """The one class replay corrupts. `streamed += delta` double-appends, and a delta is
    meaningless alone — it is superseded by the completed `assistant` frame that follows."""
    delta = {"type": "stream_event", "uuid": "message-1", "event": {"type": "content_block_delta"}}

    assert frame_uid("stream_event", delta) is None


def test_what_the_console_authored_itself_has_none() -> None:
    """Neither crosses the bridge as a conversation frame, so neither is ever replayed."""
    assert frame_uid("setup_output", {"text": "cloning"}) is None
    assert frame_uid("partial", {"type": "assistant"}) is None


def test_a_kind_this_release_does_not_know_has_none() -> None:
    """None is the safe direction. A duplicate that slips past is one repeated line in a rollout;
    an identity invented to avoid that would drop a frame that never arrived twice."""
    assert frame_uid("something_later", {"id": "x"}) is None


def test_a_frame_missing_the_field_its_kind_is_identified_by_has_none() -> None:
    assert frame_uid("assistant", {"type": "assistant"}) is None
    assert frame_uid("result", {"type": "result", "subtype": "success"}) is None


if __name__ == "__main__":
    pytest_bazel.main()
