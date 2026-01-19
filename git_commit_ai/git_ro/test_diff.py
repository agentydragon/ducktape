from __future__ import annotations

import pytest_bazel

from git_commit_ai.git_ro.formatting import ListSlice, TextSlice
from git_commit_ai.git_ro.server import DiffFormat, DiffInput, TextPage


async def test_git_diff_patch_first_page(typed_git_ro) -> None:
    union = await typed_git_ro.diff(
        DiffInput(
            format=DiffFormat.PATCH,
            staged=True,
            unified=0,
            rev_a=None,
            rev_b=None,
            paths=None,
            find_renames=True,
            slice=TextSlice(offset_chars=0, max_chars=2000),
            list_slice=ListSlice(offset=0, limit=100),
        )
    )
    assert isinstance(union, TextPage)
    # Page fields are on the union directly
    assert union.truncated is True
    assert isinstance(union.next_offset, int)
    assert union.next_offset > 0


async def test_git_diff_patch_second_page(typed_git_ro) -> None:
    union1 = await typed_git_ro.diff(
        DiffInput(
            format=DiffFormat.PATCH,
            staged=True,
            unified=0,
            rev_a=None,
            rev_b=None,
            paths=None,
            find_renames=True,
            slice=TextSlice(offset_chars=0, max_chars=2000),
            list_slice=ListSlice(offset=0, limit=100),
        )
    )
    next_offset = union1.next_offset or 0

    union2 = await typed_git_ro.diff(
        DiffInput(
            format=DiffFormat.PATCH,
            staged=True,
            unified=0,
            rev_a=None,
            rev_b=None,
            paths=None,
            find_renames=True,
            slice=TextSlice(offset_chars=next_offset, max_chars=2000),
            list_slice=ListSlice(offset=0, limit=100),
        )
    )
    assert isinstance(union2, TextPage)
    assert union2.total_chars == union1.total_chars
    assert union2.body != union1.body


if __name__ == "__main__":
    pytest_bazel.main()
