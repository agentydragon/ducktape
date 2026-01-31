"""
Patch utilities:
- apply_patch_auto: apply an OpenAI patch envelope via IO callbacks
"""

from __future__ import annotations

from collections.abc import Callable

from third_party.openai_cookbook.apply_patch import identify_files_needed, process_patch

# Canonical error message when patches must modify exactly one file
SINGLE_FILE_REQUIRED_ERR = "patch must modify exactly one file"


def apply_patch_auto(
    patch_text: str,
    open_fn: Callable[[str], str],
    write_fn: Callable[[str, str], None],
    remove_fn: Callable[[str], None],
    *,
    require_single_file: bool | None = None,
) -> tuple[dict[str, str], set[str]]:
    written: dict[str, str] = {}
    removed: set[str] = set()

    def _wrap_write(path: str, content: str) -> None:
        write_fn(path, content)
        written[path] = content

    def _wrap_remove(path: str) -> None:
        remove_fn(path)
        removed.add(path)

    files = identify_files_needed(patch_text)
    if require_single_file and len(files) != 1:
        raise ValueError(SINGLE_FILE_REQUIRED_ERR)
    process_patch(patch_text, open_fn, _wrap_write, _wrap_remove)

    if require_single_file:
        touched = set(written.keys()) | removed
        if len(touched) != 1:
            raise ValueError(SINGLE_FILE_REQUIRED_ERR)
    return written, removed
