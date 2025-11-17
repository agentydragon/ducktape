from __future__ import annotations

from adgn.mcp.git_ro.server import LogInput, StatusInput, TextPage, TextSlice


async def test_git_status_basic(typed_git_ro) -> None:
    async with typed_git_ro() as (client, _):
        sp = await client.git_status(StatusInput())
        assert hasattr(sp, "entries")
        assert isinstance(sp.entries, list)


async def test_git_log_oneline_basic(typed_git_ro) -> None:
    async with typed_git_ro() as (client, _):
        tp: TextPage = await client.git_log(
            LogInput(rev="HEAD", max_count=5, oneline=True, slice=TextSlice(offset_chars=0, max_chars=1000))
        )
        assert isinstance(tp.body, str)
