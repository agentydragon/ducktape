# CI Firecracker / Bazel Analysis Cache Profile

`runner_recycle_stats.py` is a historical classifier for BuildBuddy runner
invocations: it samples `HOSTED_BAZEL` `remote ...` runner logs and reports
whether each took the existing-workspace ("warm") setup path or the
fresh-clone ("cold") path.

```bash
# Historical setup-path sample over the densest recent window
devinfra/x/ci_build_profile_analysis/runner_recycle_stats.py --count 600

# Wider window (API pages by recency; larger N == more hours)
devinfra/x/ci_build_profile_analysis/runner_recycle_stats.py --count 8000

# Only classify cold-start candidates: first runner after each >10min idle gap
devinfra/x/ci_build_profile_analysis/runner_recycle_stats.py --count 8000 --gaps-only
```

Needs `BUILDBUDDY_API_KEY` plus `bb` and `bbapi` on `PATH`.

A warm runner header (`Syncing existing repo...`) is only a proxy for outer
Firecracker VM reuse — it does not by itself prove the inner Bazel server's
analysis cache survived between CI runs. The 2026-06 profiling investigation
(notes in git history, removed 2026-08) settled it qualitatively:

- Firecracker resume: yes; snapshot fan-out likely and source-supported.
- The inner Bazel analysis cache is not flushed wholesale, but not perfectly
  quiescent either — the first `test //...` still does incremental
  analysis/Skyframe work.
- CI's slow critical path is remote execution of a few long tests/mypy
  actions, not analysis or package loading.

Its instrumentation survives in `.github/workflows/bazel-ci.yml`
(`devinfra/ci/bb_runner_probe.py`, `emit_bb_remote_linkage.py`), and it led to
PR CI becoming changed-file scoped via bazel-diff in `devinfra/ci/bazel_ci.sh`.
