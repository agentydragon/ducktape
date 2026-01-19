from __future__ import annotations

import pytest_bazel

from git_commit_ai.git_ro.formatting import ListSlice, TextSlice
from git_commit_ai.git_ro.server import LogInput, StatusInput, TextPage


async def test_git_status_basic(typed_git_ro) -> None:
    sp = await typed_git_ro.status(StatusInput(list_slice=ListSlice(offset=0, limit=100)))
    assert isinstance(sp.entries, dict)


async def test_git_log_oneline_basic(typed_git_ro) -> None:
    tp: TextPage = await typed_git_ro.log(
        LogInput(rev="HEAD", max_count=5, oneline=True, slice=TextSlice(offset_chars=0, max_chars=1000))
    )
    assert isinstance(tp.body, str)


if __name__ == "__main__":
    pytest_bazel.main()
