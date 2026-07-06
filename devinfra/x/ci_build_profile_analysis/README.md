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
analysis cache survived between CI runs. For the full investigation into
whether it does, the evidence gathered, and the resulting conclusions, see
<../../debug/ci_build_profile_analysis_2026_06_10.md>.
