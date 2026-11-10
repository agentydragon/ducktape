@README.md

- A test writing screenshot output records needs the `visual` tag so the
  determinism sweep includes it; diagnostic-only producers do not.
- Determinism sweep: if a repeated `bbr test` attempt leaves no BuildBuddy
  invocation, keep completed runs (compare only with ≥2), report the command and exit
  code, and fail the sweep — don't retry or count its absent outputs as missing
  screenshots.
- Fresh repetitions: `--nocache_test_results`, not `--noremote_accept_cached` (which
  also drops prerequisite build cache hits).
