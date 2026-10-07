# pyright_watch_report.py specimen

## Context

- Snapshot date: 2025-08-29

## TODO (future properties to document and enforce)

- python/scoped-try-except.md: Broad try/except blocks; should scope to specific exceptions and smallest necessary
  block.
- High‑level property gap (planned): No footguns, clear/unambiguous outputs
  - Outputs should be clear, correct, and unsurprising to readers. When behavior depends on a mode (e.g., first‑match vs
    all‑matches), that mode must be explicitly surfaced in output/docs to avoid confusion, especially under overlapping
    patterns.
  - This specimen risks confusion by reporting per‑pattern counts without stating the attribution mode. Capture this
    under a future property (e.g., properties/no-footguns.md) to require unambiguous labeling and documentation of such
    choices.
