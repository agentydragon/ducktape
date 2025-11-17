from __future__ import annotations

from adgn.mcp.git_ro.server import ChangedFilesPage, DiffFormat, DiffStatPage, ListSlice, ShowInput, TextPage


async def test_git_show_name_status(typed_git_ro) -> None:
    async with typed_git_ro() as (client, session):
        ns_union = await client.git_show(
            ShowInput(object="HEAD", format=DiffFormat.NAME_STATUS, list_slice=ListSlice(offset=0, limit=100))
        )
        assert isinstance(ns_union, ChangedFilesPage)
        assert ns_union.items


async def test_git_show_stat(typed_git_ro) -> None:
    async with typed_git_ro() as (client, session):
        st_union = await client.git_show(
            ShowInput(object="HEAD", format=DiffFormat.STAT, list_slice=ListSlice(offset=0, limit=100))
        )
        assert isinstance(st_union, DiffStatPage)
        assert st_union.items


async def test_git_show_patch(typed_git_ro) -> None:
    async with typed_git_ro() as (client, session):
        pt_union = await client.git_show(ShowInput(object="HEAD", format=DiffFormat.PATCH))
        assert isinstance(pt_union, TextPage)
        assert isinstance(pt_union.body, str)
