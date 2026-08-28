# Gazelle Convergence

Goal: `bb run //devinfra:gazelle` completes, a rerun is a no-op, and CI enforces clean
diffs — a BUILD delta then only ever means real dependency drift. All but the last
holds: the migrations and repo-wide run have landed, conventions and mechanism have
graduated to STYLE.md § Gazelle-managed Python BUILD files and <../docs/gazelle.md>,
and the released binary (`gazelle` pin) is on the devshell PATH.

## Burn-down

1. **The drift check as its own CI signal**: a plain-runner workflow fetches the
   pinned binary and runs `--mode=diff` — no Bazel, no BuildBuddy, and red neither
   blocks nor delays bazel-ci. Content already written at commit `12395889`
   (workflow + fetch/skip script + enforcement docs). Land it, state the enforcement
   in README/STYLE/<../docs/gazelle.md>, and delete this plan.
