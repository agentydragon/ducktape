local I = import '../../specimens/lib.libsonnet';

// iss-005: Clarify accounting mode (first-match vs all-matches)

I.issueOneOccurrence(
  rationale=|||
    Clarify accounting: first‑match vs all‑matches

    Overlapping patterns mean a file may match multiple include/exclude patterns.
    There are at least 2 reasonable valid attribution modes:
    - First‑match wins (config order): attribute a file to the first include pattern that matches. Useful for "unique additional" counts; order‑sensitive and easy to explain.
    - All‑matches: count a file under every pattern it matches. Useful for coverage/overlap analysis; order‑insensitive.

    In the code as written, first‑match wins (order‑sensitive).
    All‑matches would have been a valid alternative; as such, the semantics of attribution stats is not obvious if it does not state the attribution mode.

    Document the chosen mode in output to avoid confusion.
  |||,
  properties=['truthfulness'],
  filesToRanges={
    'pyright_watch_report.py': [],  // File-wide documentation issue
  },
)
