"""
Unified diff applier (multi-file) using the unidiff parser with strict context.

- Parsing: unidiff.PatchSet
- Application: our strict, in-memory hunk application (no fuzz)
"""

from __future__ import annotations

from collections.abc import Callable

from unidiff import PatchSet


def normalize_single_file_unified_patch(text: str) -> str:
    """Normalize a unified diff for a single file.

    - Strips leading whitespace lines
    - If the patch is a hunk-only form (starts with '@@'), synthesize file headers
      using a placeholder path so unidiff can parse it.
    """
    s = text.lstrip()
    if s.startswith("@@"):
        return f"--- a/_\n+++ b/_\n{s}"
    return s


def _strip_ab_prefix(path: str) -> str:
    return path[2:] if path.startswith(("a/", "b/")) else path


def _apply_hunks_strict(original: str, file_patch) -> str:
    """Apply a unidiff file hunk list to original text with strict matching."""
    orig_lines = original.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    orig_idx = 1  # 1-based to match unidiff

    for hunk in file_patch:
        # Copy pre-hunk unchanged lines
        pre_limit = max(1, hunk.source_start)
        while orig_idx < pre_limit and (orig_idx - 1) < len(orig_lines):
            out.append(orig_lines[orig_idx - 1])
            orig_idx += 1

        for line in hunk:
            # unidiff preserves trailing newlines in line.value
            val = line.value.rstrip("\n")
            if line.is_context:
                if (orig_idx - 1) >= len(orig_lines) or orig_lines[orig_idx - 1] != val:
                    raise ValueError("unified diff: context mismatch")
                out.append(val)
                orig_idx += 1
            elif line.is_removed:
                if (orig_idx - 1) >= len(orig_lines) or orig_lines[orig_idx - 1] != val:
                    raise ValueError("unified diff: deletion mismatch")
                orig_idx += 1
            elif line.is_added:
                out.append(val)
            else:
                raise ValueError("unified diff: unexpected hunk line kind")

    # Copy remaining original
    while (orig_idx - 1) < len(orig_lines):
        out.append(orig_lines[orig_idx - 1])
        orig_idx += 1

    return "\n".join(out)


def apply_unified_diff(
    text: str, open_fn: Callable[[str], str], write_fn: Callable[[str, str], None], remove_fn: Callable[[str], None]
) -> None:
    norm = normalize_single_file_unified_patch(text)
    patch = PatchSet(norm)
    for fp in patch:
        is_add = fp.is_added_file
        is_del = fp.is_removed_file
        # target path for write/update; source path for delete
        target = fp.target_file if not is_del else fp.source_file
        path = _strip_ab_prefix(target)

        if is_del:
            original = open_fn(path)
            # Apply strictly, even though we ultimately remove the file; allows consistency checks
            _apply_hunks_strict(original, fp)
            remove_fn(path)
            continue

        original = "" if is_add else open_fn(path)
        new_text = _apply_hunks_strict(original, fp)
        write_fn(path, new_text)
