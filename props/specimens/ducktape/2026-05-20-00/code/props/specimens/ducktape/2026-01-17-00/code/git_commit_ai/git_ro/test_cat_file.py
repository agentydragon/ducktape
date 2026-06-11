from __future__ import annotations

import pytest

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


async def test_cat_file_not_found(typed_git_ro) -> None:
    """FileNotFoundError for missing path."""
    with pytest.raises(FileNotFoundError, match="Path not found"):
        await typed_git_ro.cat_file(
            CatFileInput(object="HEAD:nonexistent.txt", slice=TextSlice(offset_chars=0, max_chars=100))
        )


async def test_cat_file_index_not_found(typed_git_ro) -> None:
    """FileNotFoundError for missing index entry."""
    with pytest.raises(FileNotFoundError, match="Index entry not found"):
        await typed_git_ro.cat_file(
            CatFileInput(object=":nonexistent.txt", slice=TextSlice(offset_chars=0, max_chars=100))
        )


async def test_conflict_stage1_ancestor(typed_git_ro_conflict) -> None:
    """Read ancestor (stage 1) from merge conflict."""
    result = await typed_git_ro_conflict.cat_file(
        CatFileInput(object=":1:conflict.txt", slice=TextSlice(offset_chars=0, max_chars=100))
    )
    assert isinstance(result, TextPage)
    assert "ancestor content" in result.body


async def test_conflict_stage2_ours(typed_git_ro_conflict) -> None:
    """Read ours (stage 2) from merge conflict."""
    result = await typed_git_ro_conflict.cat_file(
        CatFileInput(object=":2:conflict.txt", slice=TextSlice(offset_chars=0, max_chars=100))
    )
    assert isinstance(result, TextPage)
    assert "ours content" in result.body


async def test_conflict_stage3_theirs(typed_git_ro_conflict) -> None:
    """Read theirs (stage 3) from merge conflict."""
    result = await typed_git_ro_conflict.cat_file(
        CatFileInput(object=":3:conflict.txt", slice=TextSlice(offset_chars=0, max_chars=100))
    )
    assert isinstance(result, TextPage)
    assert "theirs content" in result.body


async def test_conflict_stage0_not_found(typed_git_ro_conflict) -> None:
    """Stage 0 doesn't exist for conflicted files."""
    with pytest.raises(FileNotFoundError, match="Index entry not found"):
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
    with pytest.raises(FileNotFoundError, match="not found at repository root"):
        await typed_git_ro_new_file.cat_file(
            CatFileInput(object="HEAD:src/newfile.py", slice=TextSlice(offset_chars=0, max_chars=100))
        )


async def test_path_error_shows_available_entries(typed_git_ro_new_file) -> None:
    """Error message shows available entries when path component not found."""
    with pytest.raises(FileNotFoundError, match=r"Entries at repository root:.*README.md"):
        await typed_git_ro_new_file.cat_file(
            CatFileInput(object="HEAD:nonexistent/file.py", slice=TextSlice(offset_chars=0, max_chars=100))
        )


async def test_bare_filename_error_message(typed_git_ro) -> None:
    """Helpful error when using bare filename instead of full path."""
    with pytest.raises(FileNotFoundError, match="Path must be relative to repository root"):
        await typed_git_ro.cat_file(CatFileInput(object="HEAD:README", slice=TextSlice(offset_chars=0, max_chars=100)))
