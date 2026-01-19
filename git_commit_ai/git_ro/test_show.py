from __future__ import annotations

import pytest_bazel

from git_commit_ai.git_ro.formatting import ListSlice, TextSlice
from git_commit_ai.git_ro.server import ChangedFilesPage, DiffFormat, DiffStatPage, ShowInput, TextPage


async def test_git_show_name_status(typed_git_ro) -> None:
    ns_union = await typed_git_ro.show(
        ShowInput(
            object="HEAD",
            format=DiffFormat.NAME_STATUS,
            slice=TextSlice(offset_chars=0, max_chars=0),
            list_slice=ListSlice(offset=0, limit=100),
        )
    )
    assert isinstance(ns_union, ChangedFilesPage)
    assert ns_union.items


async def test_git_show_stat(typed_git_ro) -> None:
    st_union = await typed_git_ro.show(
        ShowInput(
            object="HEAD",
            format=DiffFormat.STAT,
            slice=TextSlice(offset_chars=0, max_chars=0),
            list_slice=ListSlice(offset=0, limit=100),
        )
    )
    assert isinstance(st_union, DiffStatPage)
    assert st_union.items


async def test_git_show_patch(typed_git_ro) -> None:
    pt_union = await typed_git_ro.show(
        ShowInput(
            object="HEAD",
            format=DiffFormat.PATCH,
            slice=TextSlice(offset_chars=0, max_chars=0),
            list_slice=ListSlice(offset=0, limit=100),
        )
    )
    assert isinstance(pt_union, TextPage)
    assert isinstance(pt_union.body, str)


if __name__ == "__main__":
    pytest_bazel.main()
