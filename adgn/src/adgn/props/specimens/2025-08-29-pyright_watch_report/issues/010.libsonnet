local I = import '../../specimens/lib.libsonnet';

// iss-010: Repeated periodic progress-logging block

I.issueOneOccurrence(
  rationale=|||
    Repeated periodic progress-logging block.

    The same 3-line progress-logging snippet is duplicated in four places inside
    gather_files_single_pass. Extract a small helper to reduce duplication,
    make intent obvious, and avoid copy/paste drift.

    Before (examples taken verbatim from the specimen):

    ```python
    if progress and time.monotonic() - last_print >= 1.0:
        sys.stderr.write(
            f"scan dirs={scanned_dirs} files={scanned_files} kept={len(kept_union)} at {rp}\n",
        )
        sys.stderr.flush()
        last_print = time.monotonic()
    ```

    This is repeated at least 4 times.

    After (extract a helper):

    ```python
    def maybe_log_progress() -> None:
        nonlocal last_print
        if progress and time.monotonic() - last_print >= 1.0:
            sys.stderr.write(
                f"scan dirs={scanned_dirs} files={scanned_files} kept={len(kept_union)} at {rp}\n",
            )
            sys.stderr.flush()
            last_print = time.monotonic()

    # Then in the code:
    maybe_log_progress()
    ```

    This makes intent clear and reduces duplication.
  |||,
  properties=['no-oneoff-vars-and-trivial-wrappers'],
  filesToRanges={
    'pyright_watch_report.py': [165, 179, 196, 241, 253],
  },
)
