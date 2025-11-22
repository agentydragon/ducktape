local I = import '../../specimens/lib.libsonnet';

// iss-012: Use dict comprehension for per_include_kept

I.issueOneOccurrence(
  rationale=|||
    Use a dict comprehension for per_include_kept to reduce ephemeral state and make intent clear.

    Before (pyright_watch_report.py original):

    ```python
    per_include_kept: Dict[str, int] = {}
    for pat in include:
        per_include_kept[pat] = sum(1 for p in kept_union if matches_any(rel(p, root), [pat]))
    ```

    After (preferred):

    ```python
    per_include_kept: dict[str, int] = {
        pat: sum(1 for p in kept_union if matches_any(rel(p, root), [pat]))
        for pat in include
    }
    ```

    This is clearer, fewer moving parts, and avoids an imperative accumulation pattern.
  |||,
  properties=['python/modern-python-idioms'],
  filesToRanges={
    'pyright_watch_report.py': [[228, 231]],
  },
)
