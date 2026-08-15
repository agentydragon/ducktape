"""Reading a value out of a frame the CLI sent."""

from __future__ import annotations

import pytest_bazel

from haku.console.x.session_frames import text_delta


def test_text_delta_ignores_non_text_stream_events() -> None:
    assert text_delta({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hi"}}) == "hi"
    assert text_delta({"type": "content_block_delta", "delta": {"type": "input_json_delta"}}) == ""
    assert text_delta({"type": "message_start"}) == ""


if __name__ == "__main__":
    pytest_bazel.main()
