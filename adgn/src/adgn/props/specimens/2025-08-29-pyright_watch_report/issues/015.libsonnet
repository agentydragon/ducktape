local I = import '../../specimens/lib.libsonnet';

// iss-015: Final dump error handling should not swallow exceptions

I.issueOneOccurrence(
  rationale=|||
    Final dump error handling should not swallow exceptions.

    The end-of-program dump currently catches Exception, prints a short warning, and allows the program to exit 0. This hides real failures (permission errors, full disk, etc.) and removes useful stack traces.

    Before (pyright_watch_report.py lines ~292–301):

    ```python
    try:
        dump_path.parent.mkdir(parents=True, exist_ok=True)
        with dump_path.open("w", encoding="utf-8") as f:
            for p in sorted(kept_union):
                f.write(str(p) + "\n")
        sys.stderr.write(f"Written {len(kept_union)} paths to {dump_path}\n")
    except Exception as e:
        sys.stderr.write(f"Warning: failed to write dump: {e}\n")
    ```

    After (recommended): Let exceptions propagate so failures are visible:

    ```python
    dump_path.parent.mkdir(parents=True, exist_ok=True)
    with dump_path.open("w", encoding="utf-8") as f:
        for p in sorted(kept_union):
            f.write(str(p) + "\n")
    sys.stderr.write(f"Written {len(kept_union)} paths to {dump_path}\n")
    ```

    This makes real problems visible and avoids silent data loss.
  |||,
  properties=['python/no-swallowing-errors'],
  filesToRanges={
    'pyright_watch_report.py': [[292, 301]],
  },
)
