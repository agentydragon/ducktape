# Session Start Simplification — Remaining

Most of this plan is done. See git history for details on completed work (secrets
refactor, background tasks generalization, shim refactor, CI workflow dedup, profile
config consolidation, CI `bbr` removal, per-profile session context, recovery doc update).

## Maybe: `ci_env.sh` as full CI setup step

CI-only (not web, not CLI). `ci_env.sh` could do more than export vars:

- Registry logins (`docker login`) instead of `GHCR_*` env vars
- `GITHUB_TOKEN` in CI should be the release PAT, not the agent PAT
- Requires auditing all in-repo consumers of these env vars before rewiring

## Low priority: statusline packaging separation

The statusline hook is installed system-wide (home-manager) so it works in non-ducktape
sessions, but this causes two `claude-hooks` installations on dev machines (home-manager +
devShell/envrc). Cleaner fix: extract statusline into its own package. Low priority — the
dual install works, just wastes a Nix closure.

## Low priority: `env_script_exports` threading

`env_script_exports: str` is threaded through `app.state` → `handle_session_start()` →
session env file. Could be cached alongside the profile instead. Current threading works.

## Minor TODOs

- Double env_script resolution on laptop (direnv + daemon startup) — no-op, cosmetic
- `DUCKTAPE_DOCKER_CLIENT_KEY` disabled in `_common.sh` — blocked on docker-ci cluster
  accessibility from RBE workers
- `bbr.py` forwards `DUCKTAPE_DOCKER_CLIENT_KEY` via `--remote_run_header` which overrides
  `--test_env` — can't unset from inner bazel flags alone

## Non-goals

- Replacing SOPS with another secret backend
- Changing the hook daemon architecture (UDS, per-session state, FastAPI)
- Rewriting the proxy/BES interceptor
