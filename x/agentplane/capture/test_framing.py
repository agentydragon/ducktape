import pytest_bazel

from x.agentplane.capture.framing import NewlineFramer


def test_framer_preserves_crlf_and_eof_tail() -> None:
    framer = NewlineFramer()
    assert framer.feed(b'{"a":') == []
    assert framer.feed(b"null}\r\nmalformed") == [(b'{"a":null}', b"\r\n", False)]
    assert framer.finish() == [(b"malformed", b"", True)]


if __name__ == "__main__":
    pytest_bazel.main()
