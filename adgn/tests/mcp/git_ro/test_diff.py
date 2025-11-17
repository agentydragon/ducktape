from __future__ import annotations

from adgn.mcp.git_ro.server import DiffInput, TextPage, TextSlice


async def test_git_diff_patch_first_page(typed_git_ro) -> None:
    async with typed_git_ro() as (client, session):
        union = await client.git_diff(
            DiffInput(staged=True, unified=0, slice=TextSlice(offset_chars=0, max_chars=2000))
        )
        assert isinstance(union, TextPage)
        # Page fields are on the union directly
        assert union.truncated is True
        assert isinstance(union.next_offset, int)
        assert union.next_offset > 0


async def test_git_diff_patch_second_page(typed_git_ro) -> None:
    async with typed_git_ro() as (client, session):
        union1 = await client.git_diff(
            DiffInput(staged=True, unified=0, slice=TextSlice(offset_chars=0, max_chars=2000))
        )
        next_offset = union1.next_offset or 0

        union2 = await client.git_diff(
            DiffInput(staged=True, unified=0, slice=TextSlice(offset_chars=next_offset, max_chars=2000))
        )
        assert isinstance(union2, TextPage)
        assert union2.total_chars == union1.total_chars
        assert union2.body != union1.body
