local I = import '../../specimens/lib.libsonnet';

// iss-007: Compute include impacts during the scan (not post-hoc)

I.issueOneOccurrence(
  rationale=|||
    Account include and exclude pattern hits symmetrically: accumulate include stats inside the main walk instead of reconstructing later.
    This avoids a second pass and keeps ordering semantics obvious.

    Specimen's post-hoc reconstruction of include hits from `kept_union`:

    ```python
    per_include_kept: Dict[str, int] = {}
    for pat in include:
      per_include_kept[pat] = sum(1 for p in kept_union if matches_any(rel(p, root), [pat]))
    # and a separate loop to compute "unique additional" with a seen set
    ```

    Better (gather hits during the scan):

    ```python
    include_hits = Counter()                 # all includes that match a file
    per_include_unique: dict[str, set[Path]] = {pat: set() for pat in include}
    seen: set[Path] = set()
    ...
    # Inside the os.walk loop, after rp = rel(p, root)
    matches = [pat for pat in include if matches_any(rp, [pat])]
    include_hits.update(matches)
    # First-match wins for order-sensitive "unique additional" counting
    for pat in matches:
      if p not in seen:
          per_include_unique[pat].add(p)
          seen.add(p)
      break
    ```

    This reduces moving parts and cognitive load needed do understand the pipeline.
  |||,
  properties=['python/modern-python-idioms'],
  filesToRanges={
    'pyright_watch_report.py': [218, 236],
  },
)
