# tools/ — run helpers

Reusable scripts that bake the fiddly, every-run rituals into one command, so a parsing
slip or a missed guard can't silently lose data. **All fail loud** (non-zero exit + the
real error); none swallow a token expiry or API gap.

This is STARTER tooling: fill in the placeholders (`<agent-namespace>`, `<git-write-secret>`,
`<owner>`, `your-forgejo.example.com`) or set the documented env vars for your instance.

| Tool                | What it does                                                                                                                                                                                                                   | When                              |
| ------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | --------------------------------- |
| `validate_local.sh` | Runs `validate_state.py` against `ui/backend/models.py` (exit 1 = invalid state, exit 2 = env can't run it → `push_state` warns + relies on CI).                                                                               | via `push_state.sh`               |
| `push_state.sh`     | Pre-push state validation (blocks on invalid), then `git push` to `origin/main`; auto-falls-back to an in-cluster pod-bundle push only on the egress-stall signature. `CI_WAIT=1` chains `ci_wait.sh` after a successful push. | end of every run                  |
| `ci_wait.sh`        | Blocks until every Forgejo Actions run for local HEAD concludes; exit 1 on failure, 2 on can't-verify.                                                                                                                         | after the final push of every run |

Usage examples:

```bash
tools/validate_local.sh
tools/push_state.sh
CI_WAIT=1 tools/push_state.sh   # push, then wait on CI
tools/ci_wait.sh
```

Configuration:

- `validate_local.sh` — needs a `python3` with `pydantic>=2` + `pyyaml`; override with
  `VALIDATE_PY` (e.g. `VALIDATE_PY="uv run --with 'pydantic>=2' --with pyyaml python"`).
- `ci_wait.sh` — reads `FORGEJO_API_BASE`, `FORGEJO_USER`, `FORGEJO_TOKEN` from the env
  (the header comment shows how to source them from a k8s Secret).
- `push_state.sh` — set `AGENT_NAMESPACE` (and optionally `GIT_WRITE_SECRET`) so the
  in-cluster pod fallback can find the git-write creds Secret. The direct push path needs
  neither.

Notes:

- `push_state.sh`'s direct path is exercised every run; the pod fallback only triggers on
  the intermittent egress stall, so verify it against your cluster before relying on it.
