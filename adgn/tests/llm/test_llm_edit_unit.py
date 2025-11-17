#!/usr/bin/env python3
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path

import pytest

from adgn.mcp.editor_server import DoneInput, EditorOutcome, is_python_path, make_editor_server


@pytest.fixture
def editor_session(make_typed_mcp):
    """Factory fixture yielding (TypedClient, session) for a given path using shared helper."""

    @asynccontextmanager
    async def _open(p: Path):
        server = make_editor_server(p)
        async with make_typed_mcp(server, "editor") as pair:
            yield pair

    return _open


def test_is_python_path() -> None:
    assert is_python_path(Path("foo.py"))
    assert is_python_path(Path("bar.pyi"))
    assert not is_python_path(Path("README.md"))
    assert not is_python_path(Path("Makefile"))


def _extract_result(res):
    payload = getattr(res, "structured_content", None)
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            payload = None
    if not payload:
        # Fallback: parse first text content block as JSON
        content = getattr(res, "content", None) or []
        if content and getattr(content[0], "text", None):
            try:
                payload = json.loads(content[0].text)
            except Exception:
                payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return payload.get("result", payload)


async def test_done_for_non_python_no_syntax_check(tmp_path: Path, editor_session) -> None:
    p = tmp_path / "note.md"
    await asyncio.to_thread(p.write_text, "hello\n", encoding="utf-8")

    async with editor_session(p) as (client, sess):
        # Append a line after the first line (1-based after = insert at index 1)
        await sess.call_tool(name="add_line_after", arguments={"line_number": 1, "content": "world"})
        # Finish successfully; should not run python syntax checks
        out = await client.done(DoneInput(outcome=EditorOutcome.SUCCESS, summary="ok"))
        assert (out.kind, out.summary) == ("Success", "ok")

    # file saved with edits
    content = await asyncio.to_thread(p.read_text, encoding="utf-8")
    assert content == "hello\nworld\n"


async def test_done_python_syntax_failure_returns_structured_failure(tmp_path: Path, editor_session) -> None:
    p = tmp_path / "bad.py"
    await asyncio.to_thread(p.write_text, "def f():\n    return 1\n", encoding="utf-8")  # start valid

    async with editor_session(p) as (client, sess):
        # Introduce a syntax error by replacing the function header
        await sess.call_tool(name="replace_text", arguments={"old_text": "def f():", "new_text": "def f(:"})
        out = await client.done(DoneInput(outcome=EditorOutcome.SUCCESS, summary="finish"))
        assert out.kind == "Failure"
        assert "Cannot complete" in (out.summary or "")

    # file on disk should not have been overwritten with bad content
    content = await asyncio.to_thread(p.read_text, encoding="utf-8")
    assert content == "def f():\n    return 1\n"


async def test_done_explicit_failure_reverts_in_memory(tmp_path: Path, editor_session) -> None:
    p = tmp_path / "file.txt"
    await asyncio.to_thread(p.write_text, "A\n", encoding="utf-8")

    async with editor_session(p) as (client, sess):
        # Stage change to "B" in-memory: delete line 1 and insert B at start
        await sess.call_tool(name="delete_line", arguments={"line_number": 1})
        await sess.call_tool(name="add_line_after", arguments={"line_number": 0, "content": "B"})
        out = await client.done(DoneInput(outcome=EditorOutcome.FAILURE, summary="abort"))
        assert (out.kind, out.summary) == ("Failure", "abort")
        # Ensure in-memory state reverted by reading current first line
        rr = await sess.call_tool(name="read_line_range", arguments={"start": 1, "end": 1})
        body = (rr.structured_content or {}).get("body", "")
        assert "A" in body

    # file on disk unchanged
    content = await asyncio.to_thread(p.read_text, encoding="utf-8")
    assert content == "A\n"
