local I = import '../../specimens/lib.libsonnet';

// iss-009: Inline trivial temporaries and trivial wrappers

I.issueOneOccurrence(
  rationale=|||
    Inline trivial temporaries and trivial wrappers. Avoid assigning a value to a variable if the only thing you do with it is immediately pass it on. If it does not hurt readability, just immediately inline the value where it gets used.

    **Per-include stats (kept) at lines 272-279:**
    ```python
    incl_stats: list[tuple[str, int]] = sorted(
        ((pat, per_include_kept.get(pat, 0)) for pat in include),
        key=lambda x: x[1],
        reverse=True,
    )
    for pat, cnt in incl_stats:
        print(f"  {pat:40s} {cnt:8d}")
    ```

    Better: save yourself a temp var and iterate sorted(...) directly in the for:
    ```python
    for pat, cnt in sorted(
        ((pat, per_include_kept.get(pat, 0)) for pat in include),
        key=lambda x: x[1],
        reverse=True,
    ):
        print(f"  {pat:40s} {cnt:8d}")
    ```

    **Top excludes at lines 135-139:**
    ```python
    exclude_impact: List[Tuple[str, int]] = sorted(exclude_hits.items(), key=lambda x: x[1], reverse=True)
    for pat, cnt in exclude_impact[:20]:
        print(f"  {pat:40s} -{cnt:7d}")
    ```

    Better: inline sorted slice at use site:
    ```python
    for pat, cnt in sorted(exclude_hits.items(), key=lambda x: x[1], reverse=True)[:20]:
        print(f"  {pat:40s} -{cnt:7d}")
    ```

    **Total watched files at lines 151-158:**
    ```python
    total_files = len(kept_union)
    print(f"Total watched files (approx): {total_files}")
    ```

    Better:
    ```python
    print(f"Total watched files (approx): {len(kept_union)}")
    ```

    **Inline one-off total_code variable:**
    ```python
    total_code = sum(1 for p in kept_union if p.suffix in CODE_EXTS)
    print(f"  of which code ({'/'.join(sorted(CODE_EXTS))}): {total_code}")
    ```

    Better:
    ```python
    print(f"  of which code ({'/'.join(sorted(CODE_EXTS))}): {sum(p.suffix in CODE_EXTS for p in kept_union)}")
    ```

    **`dp = Path(dirpath)` at lines 228-231:**
    ```python
    for dirpath, dirnames, filenames in os.walk(root):
        dp = Path(dirpath)
        for fn in filenames:
            p = dp / fn
    ```

    Better:
    ```python
    for dirpath, dirnames, filenames in os.walk(root):
        for fn in filenames:
            p = Path(dirpath) / fn
    ```

    **Redundant temp var `matched_any_excl` at lines 246-251:**
    ```python
    hits = [pat for pat in exclude if matches_any(rp, [pat])]
    exclude_hits.update(hits)
    matched_any_excl = bool(hits)
    ...
    if matched_any_excl:
        progress_log(f"scan dirs={scanned_dirs} files={scanned_files} kept={len(kept_union)} at {rp}")
        continue
    ```

    Better: inline the condition:
    ```python
    hits = [pat for pat in exclude if matches_any(rp, [pat])]
    exclude_hits.update(hits)
    ...
    if hits:  # same truthiness, fewer moving parts
        progress_log(f"scan dirs={scanned_dirs} files={scanned_files} kept={len(kept_union)} at {rp}")
        continue
    ```
  |||,
  properties=['no-oneoff-vars-and-trivial-wrappers'],
  filesToRanges={
    'pyright_watch_report.py': [
      [272, 279],  // incl_stats
      [135, 139],  // exclude_impact
      [151, 158],  // total_files
      // total_code (line not specified in original)
      [228, 231],  // dp = Path(dirpath)
      [246, 251],  // matched_any_excl
      [253, 256],  // Periodic progress logging
    ],
  },
)
