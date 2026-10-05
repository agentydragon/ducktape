from __future__ import annotations

import pytest
import pytest_bazel
from fastmcp.exceptions import ToolError

from git_commit_ai.git_ro.formatting import TextSlice
from git_commit_ai.git_ro.server import CatFileInput, TextPage


async def test_cat_file_rev_path(typed_git_ro) -> None:
    """Read blob from commit tree via REV:path."""
    result = await typed_git_ro.cat_file(
        CatFileInput(object="HEAD:README.md", slice=TextSlice(offset_chars=0, max_chars=1000))
    )
    assert isinstance(result, TextPage)
    assert "hello" in result.body


async def test_cat_file_index_stage0(typed_git_ro) -> None:
    """Read blob from index via :path (stage 0)."""
    result = await typed_git_ro.cat_file(
        CatFileInput(object=":big.txt", slice=TextSlice(offset_chars=0, max_chars=100))
    )
    assert isinstance(result, TextPage)
    assert "line 0" in result.body


async def test_cat_file_index_explicit_stage0(typed_git_ro) -> None:
    """Read blob from index via :0:path (explicit stage 0)."""
    result = await typed_git_ro.cat_file(
        CatFileInput(object=":0:big.txt", slice=TextSlice(offset_chars=0, max_chars=100))
    )
    assert isinstance(result, TextPage)
    assert "line 0" in result.body


async def test_cat_file_commit_object(typed_git_ro) -> None:
    """Read raw commit object by ref."""
    result = await typed_git_ro.cat_file(CatFileInput(object="HEAD", slice=TextSlice(offset_chars=0, max_chars=1000)))
    assert isinstance(result, TextPage)
    assert "tree " in result.body
    assert "author " in result.body


async def test_cat_file_tree_object(typed_git_ro) -> None:
    """Read tree object listing."""
    result = await typed_git_ro.cat_file(
        CatFileInput(object="HEAD^{tree}", slice=TextSlice(offset_chars=0, max_chars=2000))
    )
    assert isinstance(result, TextPage)
    # Tree listing has filemode, type, oid, name
    assert "blob" in result.body
    assert "README.md" in result.body


async def test_cat_file_index_not_found(typed_git_ro) -> None:
    """ToolError wrapping FileNotFoundError for missing index entry."""
    with pytest.raises(ToolError, match=r":nonexistent\.txt"):
        await typed_git_ro.cat_file(
            CatFileInput(object=":nonexistent.txt", slice=TextSlice(offset_chars=0, max_chars=100))
        )


@pytest.mark.parametrize(("stage", "content"), [(1, "ancestor content"), (2, "ours content"), (3, "theirs content")])
async def test_conflict_stage_reads_its_side(typed_git_ro_conflict, stage: int, content: str) -> None:
    """Stages 1/2/3 of a merge conflict are the ancestor, ours and theirs."""
    result = await typed_git_ro_conflict.cat_file(
        CatFileInput(object=f":{stage}:conflict.txt", slice=TextSlice(offset_chars=0, max_chars=100))
    )
    assert isinstance(result, TextPage)
    assert content in result.body


async def test_conflict_stage0_not_found(typed_git_ro_conflict) -> None:
    """Stage 0 doesn't exist for conflicted files."""
    with pytest.raises(ToolError, match=r":0:conflict\.txt"):
        await typed_git_ro_conflict.cat_file(
            CatFileInput(object=":0:conflict.txt", slice=TextSlice(offset_chars=0, max_chars=100))
        )


async def test_new_file_read_from_index(typed_git_ro_new_file) -> None:
    """Read newly added file (not in any commit) via :path."""
    result = await typed_git_ro_new_file.cat_file(
        CatFileInput(object=":src/newfile.py", slice=TextSlice(offset_chars=0, max_chars=1000))
    )
    assert isinstance(result, TextPage)
    assert "new file content" in result.body
    assert "print('hello')" in result.body


async def test_new_file_not_in_commit_tree(typed_git_ro_new_file) -> None:
    """HEAD:path fails for newly added file not yet committed."""
    with pytest.raises(ToolError, match="src"):
        await typed_git_ro_new_file.cat_file(
            CatFileInput(object="HEAD:src/newfile.py", slice=TextSlice(offset_chars=0, max_chars=100))
        )


async def test_path_error_shows_available_entries(typed_git_ro_new_file) -> None:
    """The error lists the entries available at the failing level."""
    with pytest.raises(ToolError, match=r"README\.md"):
        await typed_git_ro_new_file.cat_file(
            CatFileInput(object="HEAD:nonexistent/file.py", slice=TextSlice(offset_chars=0, max_chars=100))
        )


if __name__ == "__main__":
    pytest_bazel.main()
