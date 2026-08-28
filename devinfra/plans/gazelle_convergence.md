# Gazelle Convergence

Goal: `bb run //devinfra:gazelle` completes, a rerun is a no-op, and CI enforces clean
diffs — a BUILD delta then only ever means real dependency drift. The first two hold:
the per-tree migrations and the repo-wide run have landed, and a rerun is a no-op.
Conventions and mechanism have graduated to STYLE.md § Gazelle-managed Python BUILD
files and <../docs/gazelle.md>. What remains is enforcement, sequenced so the released
binary exists before anything consumes it:

## Burn-down

1. **Release the binary** (this row's PR): `//devinfra:gazelle_python_binary` joins
   <../ci/artifact_targets.json>; a devel push then releases `gazelle-<hash>` and
   sync-pins pins it into `nix/artifact-pins.json`.
2. **Devshell package** (once the pin exists — `artifacts.gazelle` fails flake eval
   before then): a nix package over the pinned binary, into the devtools closure, so
   `gazelle` is on PATH locally and on the bbr runner.
3. **The drift check as its own CI signal**: a plain-runner workflow fetches the
   pinned binary and runs `--mode=diff` — no Bazel, no BuildBuddy, and red neither
   blocks nor delays bazel-ci. Content already written at commit `12395889`
   (workflow + fetch/skip script + enforcement docs). Land it, state the enforcement
   in README/STYLE/<../docs/gazelle.md>, and delete this plan.
