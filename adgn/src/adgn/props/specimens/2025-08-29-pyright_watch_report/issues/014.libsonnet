local I = import '../../specimens/lib.libsonnet';

// iss-014: Use comprehension + seen-update pattern for per-include unique counts

I.issueOneOccurrence(
  rationale=|||
    Use comprehension + seen-update pattern for per-include unique counts to make the code shorter and less nested while remaining readable and correct.

    Before:

    ```python
    seen: Set[Path] = set()
    per_include_unique: List[Tuple[str, int]] = []
    for pat in include:
        uniq_count = 0
        for p in sorted(kept_union):
            if p in seen:
                continue
            if matches_any(rel(p, root), [pat]):
                uniq_count += 1
                seen.add(p)
        per_include_unique.append((pat, uniq_count))
    ```

    After (shorter/flattened):

    ```python
    per_include_unique: dict[str, set[Path]] = {}
    seen: set[Path] = set()
    for pat in include:
        paths = {p for p in sorted(kept_union) if p not in seen and matches_any(rel(p, root), [pat])}
        per_include_unique[pat] = paths
        seen.update(paths)
    ```

    This primarily reduces nesting and temporary counters while keeping the same semantics.
  |||,
  properties=['python/modern-python-idioms'],
  filesToRanges={
    'pyright_watch_report.py': [[232, 244]],
  },
)
