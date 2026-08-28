# Gazelle Convergence

Goal: `bb run //devinfra:gazelle` completes, a rerun is a no-op, and CI enforces clean
diffs — a BUILD delta then only ever means real dependency drift. The first two hold:
the per-tree migrations and the repo-wide run have landed, and a rerun is a no-op.
Conventions and mechanism have graduated to STYLE.md § Gazelle-managed Python BUILD
files and <../docs/gazelle.md>. What remains is enforcement.

## Burn-down

1. **The drift check becomes its own CI signal.** Build and release the gazelle
   binary as a standalone artifact (it is a Go binary; at check time it needs only
   the checkout and the checked-in manifest — no Bazel, no BuildBuddy), pinned
   in-repo and bumped with plugin/patch changes. A separate plain-runner job runs it
   in `--mode=diff`: red means drift, and it neither blocks nor delays bazel-ci.
   Land it, then state the enforcement in README/STYLE/<../docs/gazelle.md> and
   delete this plan.
