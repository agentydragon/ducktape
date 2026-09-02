from x.agentplane.capture.llm_recording_proxy import _sse_event_name, _sse_frames, _sse_packet_matches, safe_headers


def test_safe_headers_is_header_blind() -> None:
    headers = {
        "Authorization": "Bearer not-for-recording",
        "Cookie": "no",
        "Content-Type": "application/json",
        "X-Trace": "ignored",
    }
    assert safe_headers(headers) == {"content-type": "application/json"}


def test_sse_frames_retain_a_partial_packet_until_its_boundary_arrives() -> None:
    frames, trailing = _sse_frames(b"event: message_start\ndata: {}\n\nevent: content_block_delta\ndata: {")

    assert frames == [b"event: message_start\ndata: {}\n\n"]
    assert _sse_event_name(frames[0]) == "message_start"

    frames, trailing = _sse_frames(trailing + b"}\n\n")

    assert frames == [b"event: content_block_delta\ndata: {}\n\n"]
    assert trailing == b""


def test_sse_event_name_uses_a_responses_data_type_without_an_event_header() -> None:
    assert _sse_event_name(b'data: {"type":"response.created"}\n\n') == "response.created"


def test_sse_packet_matches_a_messages_delta_type() -> None:
    frame = b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta"}}\n\n'

    assert _sse_packet_matches(frame, "text_delta")


if __name__ == "__main__":
    import pytest_bazel

    pytest_bazel.main()
