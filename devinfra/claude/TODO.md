# claude_hooks TODO

## Audit claude-hooks wheel deps after auth_proxy removal

**CLEANUP(2026-04-16)**: The `auth_proxy` subsystem was removed in the
followup to PR #1325. Several Python runtime deps may now be unused:

- `cryptography` — previously for CA extraction / x509 parsing
- `grpcio` + `protobuf` — previously for the BES interceptor
- `pyjwt` — previously for proxy JWT credential expiry checks

Check whether anything else in the wheel imports these (e.g. via transitive
use by FastAPI/uvicorn/etc.) and drop the ones that are truly unused.

Files to update in lockstep:

1. `//:claude_hooks_wheel` `requires` in root `BUILD.bazel`
2. `claude-hooks` `propagatedBuildInputs` in `nix/packages/default.nix`

Condition to remove this tombstone: deps audited and pruned, or confirmed
still needed by indirect usage.

## OTEL bearer token: mirror into SOPS

**CLEANUP(2026-04-15)**: today `devinfra/secrets/web_env.sh` fetches
`DUCKTAPE_OTEL_BEARER_TOKEN` from the `alloy-otlp-bearer-token` K8s Secret via
`kubectl get secret`. On CCR v2 web sandboxes (and any env where the k8s API
is unreachable) this blocks the daemon's `startup_env_script` for ~30s per
retry and wedges session startup — so the call is now gated behind the
`enable_k8s_otel_bearer_token` profile flag (off by default, see
`ProfileConfig`).

The real fix is to mirror the Authentik-generated token into a SOPS-encrypted
file and `try_export` from it, matching the pattern used for every other
secret. Sketch:

1. Extend `cluster/terraform/gitops/alloy-otlp-bearer-token/` to also write
   the token into `secrets/alloy-otlp-bearer-token.yaml` via `sops_file` or
   an equivalent provider — re-encrypted for the `claude-web` age recipient
   on every TF apply.
2. Swap `web_env.sh`'s `try_export_from_k8s` call for a
   `try_export "$REPO_ROOT/secrets/alloy-otlp-bearer-token.yaml" '["token"]'`.
3. Delete the `enable_k8s_otel_bearer_token` flag on `ProfileConfig` and its
   plumbing in `hook_daemon/main.py` + `web_env.sh`.

Condition to remove this tombstone: the SOPS mirror exists and `web_env.sh`
reads the token without touching kubectl.

## Nix Installation Timeout

**Problem**: Installing nix on Claude Code web times out because downloading nixpkgs takes >2 minutes (session start hook timeout).

**Current Workaround**: The `claude_hooks` package is installed via `uv tool install` from a pre-built wheel (published to GitHub releases), avoiding Python dependency installation during session start. Terraform tools (opentofu, tflint) are needed on PATH for `antonbabenko/pre-commit-terraform` hooks (`terraform_validate`, `terraform_tflint`). Nix is installed separately for `nix eval` and flake operations. Nix formatting uses a static nixfmt binary (no Nix dependency).

**Potential Solutions:**

- **Pre-built nix store tarball** (recommended) - CI builds closure, publishes tarball, session hook unpacks
- **Pre-computed store paths** - CI records paths, session hook does `nix copy`

## Auto-install Terraform Tools in Session Start Hook

**Problem**: The `terraform_tflint` and `terraform_validate` pre-commit hooks (via `antonbabenko/pre-commit-terraform`) require tflint and opentofu on PATH. On Claude Code web (gVisor sandbox), these may not be available.

**Solution**: Consider auto-installing tflint and opentofu in the session start hook, so `pre-commit run` works out of the box for terraform changes.

## Benchmark `bb remote` with and without `--config=rbe`

With warm runner VMs, `bb remote` without `--config=rbe` (local `linux-sandbox` on the
runner) may be fast enough to skip RBE entirely. Measure on a nontrivial workload: dirty a
widely-imported file (e.g., a root `conftest.py` or a core library module) to invalidate
many targets, then compare `bb remote test //... --config=rbe` vs `bb remote test //...`
(no RBE). Check wall-clock time, action count, and cache hit rate.

## Hook Daemon Lifecycle Management

**Problem**: The hook daemon client (`hook_daemon/client.py`) manually manages daemon lifecycle: pidfile read/write, process liveness checks, stale socket cleanup, fork+wait. This is ~50 lines of somewhat fiddly code.

**Potential solutions**:

- [`python-daemon`](https://pypi.org/project/python-daemon/) — handles server-side daemonization (double-fork, PID file, signal handling). Doesn't help with the client-side "ensure running" logic.
- [`zdaemon`](https://pypi.org/project/zdaemon/) — Zope-era daemon controller with start/stop/restart/status and PID management. Closest fit but adds a Zope dependency.
- **Move under supervisord** — adding the hook daemon under supervisor would eliminate the pidfile/fork logic entirely (client calls `supervisorctl start hook-daemon` if socket is dead). Trades custom lifecycle code for coupling to supervisor availability.

## Integration Test: Session Start Hook via Nix devShell

**Problem**: The container E2E test (`container_e2e/test_container_e2e.py`)
exercises the hook daemon inside a Docker container with `uv tool install`,
but does not exercise the Nix-packaged `claude-hooks` derivation. Missing
Nix-level dependencies (like `grpcio`) cause the daemon to crash with
`ModuleNotFoundError` at startup — only discovered when a real CLI session
starts.

**Solution**: Add an integration test that runs the exact session start hook
shim as configured in `.claude/settings.json` (i.e., invokes `claude-hook` the
same way Claude Code does), using the Nix devShell environment. This verifies
that all runtime imports resolve end-to-end — not just a subset.
