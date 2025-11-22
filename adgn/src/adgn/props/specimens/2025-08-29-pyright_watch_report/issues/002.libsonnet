local I = import '../../specimens/lib.libsonnet';

// iss-002: Progress interval magic number

I.issueOneOccurrence(
  rationale=|||
    Progress interval is encoded as a magic float literal `1.0` (seconds) in multiple places, which makes the unit implicit.
    Either:
    (A) Preferred: Use a duration type (e.g., `PROGRESS_INTERVAL = timedelta(seconds=1)`) and compare using datetime consistently (e.g., `last_print: datetime`, `now = datetime.now(timezone.utc)`, and `if now - last_print >= PROGRESS_INTERVAL:`).
    (B) At least add _s / _seconds / similar suffix to make unit unambiguous.

    Original (multiple places):
    ```python
    if progress and time.monotonic() - last_print >= 1.0:
        ...
        last_print = time.monotonic()
    ```
  |||,
  properties=['self-describing-names'],
  filesToRanges={
    'pyright_watch_report.py': [165, 179, 196, 241, 253],
  },
)
