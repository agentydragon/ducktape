# Bootstrap Chicken-and-Egg Problems

Instances where CI/release infrastructure depends on artifacts it produces,
creating circular dependencies that can deadlock if the chain breaks.

## 1. `bbr` in `claude-hooks` wheel

**Cycle**: CI workflows use `bbr` (installed via `nix profile install .#devtools`)
to run Bazel on RBE. `bbr` is a console script entry point in the `claude-hooks`
wheel. The `claude-hooks` wheel is built and released by CI (`.github/workflows/release.yml`).

```
CI needs bbr → bbr comes from claude-hooks wheel (artifact-pins) → wheel is released by CI using bbr
```

**Triggered 2026-04-10**: The `bbr` entry point was added to `claude-hooks` in
commit `fde2dd95` (rename `bb-remote` to `bbr`), but the last released wheel was
from commit `4563071` (before `bbr` existed). All CI jobs failed with
`bbr: command not found`.

**Recovery**: Build the wheel manually with `bb remote build //:claude_hooks_wheel`,
create a GitHub release with `gh release create`, update `nix/artifact-pins.json`.

**Prevention**: When adding new entry points to wheels that CI itself depends on,
manually release the updated wheel before merging, or ensure CI has a fallback
(e.g., `bb remote` directly).

## 2. BuildBuddy protos checksum drift

**Not a cycle**, but co-occurs: GitHub periodically regenerates `.tar.gz` archives,
changing their checksum. This breaks `http_archive` rules with pinned `integrity`.
Local builds fail; RBE builds may use cached results and appear fine.

**Fix**: Re-download and update the `integrity` hash in the `.bzl` file.

## General Mitigation

- **Smoke-test new entry points**: Before merging a commit that adds a new
  console script to a CI-critical wheel, verify that CI can still install the
  old wheel and the new entry point isn't immediately required.
- **Manual bootstrap escape hatch**: Keep `bb remote` available as a direct
  alternative to `bbr` — it's the same underlying command without the wrapper
  logic. CI could fall back to `bb remote` if `bbr` is missing.
- **Pin awareness**: When `nix/artifact-pins.json` points to a wheel, CI gets
  exactly that version. New entry points don't exist until a new release is
  pinned.
