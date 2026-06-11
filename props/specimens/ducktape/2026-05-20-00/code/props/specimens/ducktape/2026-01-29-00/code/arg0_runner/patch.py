"""
Patch utilities:
- apply_patch_auto: dispatch OpenAI patch envelope vs multi-file unified diff and apply via IO callbacks
"""

from __future__ import annotations

from collections.abc import Callable

from unidiff import PatchSet

from arg0_runner.unified_patch import apply_unified_diff, normalize_single_file_unified_patch
from third_party.openai_cookbook.apply_patch import (
    identify_files_needed as oc_identify_files_needed,
    process_patch as oc_process_patch,
)

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
    s = patch_text.lstrip()
    written: dict[str, str] = {}
    removed: set[str] = set()

    def _wrap_write(path: str, content: str) -> None:
        write_fn(path, content)
        written[path] = content

    def _wrap_remove(path: str) -> None:
        remove_fn(path)
        removed.add(path)

    if s.startswith("*** Begin Patch"):
        files = oc_identify_files_needed(patch_text)
        if require_single_file and len(files) != 1:
            raise ValueError(SINGLE_FILE_REQUIRED_ERR)
        oc_process_patch(patch_text, open_fn, _wrap_write, _wrap_remove)
    else:
        pt = normalize_single_file_unified_patch(patch_text)
        if require_single_file:
            pset = PatchSet(pt)
            if len(pset) != 1:
                raise ValueError(SINGLE_FILE_REQUIRED_ERR)
        apply_unified_diff(pt, open_fn, _wrap_write, _wrap_remove)

    if require_single_file:
        touched = set(written.keys()) | removed
        if len(touched) != 1:
            raise ValueError(SINGLE_FILE_REQUIRED_ERR)
    return written, removed
