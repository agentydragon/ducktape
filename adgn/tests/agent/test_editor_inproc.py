from __future__ import annotations

from pathlib import Path

from hamcrest import all_of, assert_that, has_properties, instance_of

from adgn.mcp.editor_server import (
    DoneInput,
    EditorOutcome,
    ReadInfoArgs,
    ReadInfoResult,
    ReplaceTextArgs,
    ReplaceTextResult,
    Success,
)


async def test_editor_inproc_basic_ops(typed_editor_factory) -> None:
    async with typed_editor_factory() as (stub, target):
        # read_info works (typed via stub)
        info = await stub.read_info(ReadInfoArgs())
        assert info == ReadInfoResult(ok=True, lines=1, path=target)

        # replace_text modifies buffer (x=1 → x=2)
        result = await stub.replace_text(ReplaceTextArgs(old_text="x = 1", new_text="x = 2"))
        assert result == ReplaceTextResult(ok=True)

        # done(success=True) runs syntax check for .py and saves
        assert_that(
            await stub.done(DoneInput(outcome=EditorOutcome.SUCCESS, summary=None)),
            all_of(instance_of(Success), has_properties(kind="Success")),
        )

    # File should be persisted with new content
    assert Path(target).read_text(encoding="utf-8") == "x = 2\n"
