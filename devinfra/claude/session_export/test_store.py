import json
import logging
from typing import Any

import pytest
import pytest_bazel

from devinfra.claude.session_export import store
from devinfra.claude.session_export.store import dumps_jsonb


@pytest.mark.parametrize(
    ("document", "stored"),
    [
        ({"a": "x\x00y"}, {"a": "x␀y"}),
        ({"\x00": ["\x00", {"n": "\x00\x00"}]}, {"␀": ["␀", {"n": "␀␀"}]}),
        # A backslash before the text `u0000` is not a NUL escape, alone or before a real one.
        ({"a": "\\u0000", "b": "\\\\u0000"}, {"a": "\\u0000", "b": "\\\\u0000"}),
        ({"a": "\\\x00"}, {"a": "\\␀"}),
        ({"a": "héllo 😀"}, {"a": "héllo 😀"}),
    ],
)
def test_dumps_jsonb_rewrites_only_nul_characters(document: dict[str, Any], stored: dict[str, Any]) -> None:
    assert json.loads(dumps_jsonb(document)) == stored


def test_dumps_jsonb_logs_how_many_characters_it_rewrote(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger=store.logger.name):
        dumps_jsonb({"a": "\x00\x00", "b": "\x00"})
    assert [record.getMessage() for record in caplog.records] == [
        "replaced 3 NUL character(s) with U+2400 in one JSON document"
    ]


if __name__ == "__main__":
    pytest_bazel.main()
