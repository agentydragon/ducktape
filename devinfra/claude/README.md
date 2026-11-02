# Claude Code Integration

Session hooks, statusline, and Claude Code API models for Claude Code sessions. What
the Rust hook daemon guarantees (CLI and web): <claude_hook/SPEC.md>; this README
covers how.

## Networking

- **Transparent MITM (classic web containers)**: no `HTTPS_PROXY`; Anthropic's
  TLS-inspection CA is already in `/etc/ssl/certs/ca-certificates.crt`, so tools work
  as-is.
- **CCR agent proxy (Claude Code Remote / remote-execution sessions)**: `HTTPS_PROXY`
  is a local TLS-re-terminating relay (`http://127.0.0.1:46587`); tools must trust
  `/root/.ccr/ca-bundle.crt` (standard `*_CA_*` env vars and system store are
  pre-set; see `/root/.ccr/README.md`). **Gotcha:** the proxy's boot-time JVM
  truststore seed races `ca-certificates-java` and often fails, so `bb`/`bbr` can't
  verify the proxy (`bazelisk` is fine). `web_setup.sh` self-heals; root cause and
  detection: <docs/ccr_bazel_truststore_race.md>.

## Hook daemon

- **PATH shims** at `<session_dir>/bin/{bazelisk,bazel,bb,bbr}` run
  `claude-hook shim <name>`; the Rust shim runtime resolves the real binary outside
  the shim dir and never calls the daemon. `bazel`/`bazelisk` inject the session bazelrc and translate
  inherited `HTTP(S)_PROXY` into Java proxy JVM properties (grpc-java → BuildBuddy);
  `bb`/`bbr` only resolve the real binary. `claude-hook` is PATH-resolved at exec
  time, so `nix profile install` / `home-manager switch` takes effect without a
  session restart.
- **`git` shim**: installed only when the active profile enables a `git_shim` flag;
  per flag it blocks `git add -A`/`git add .`, `git stash`, or `git commit --amend`.
- Installs pre-commit git hooks and writes env vars to `CLAUDE_ENV_FILE`. Hook
  registration: `.claude/settings.json`. Connectivity probe: not implemented (see
  <TODO.md>). flux, kustomize, helm come from `@multitool//tools/*`, not the
  devshell.
- **Profile**: `DUCKTAPE_CLAUDE_HOOKS_PROFILE` = path to profile YAML; schema in
  <claude_hook/config.rs>.

### Gotcha: `Setup` and `SessionStart` carry different session IDs

After compaction (observed 2026-03-21), `Setup` fires with the **new** session ID and
`SessionStart` with the **old** one (`source: compact`). The daemon is keyed by
session ID (own socket and dir), so:

- Never register `claude-hook` for `Setup` — it would start a second daemon (its
  `Setup` handler is a no-op anyway). Plain scripts that don't call `claude-hook`
  are fine: `Setup` runs `bash devinfra/claude/web_setup_hook.sh` to reinstall
  devtools.
- Session-local files (socket, shim dir, session bazelrc) are keyed by
  `SessionStart`'s ID, which may be the old one.
- Shared statusline state lives in `~/.cache/claude-hooks` (honors
  `XDG_CACHE_HOME`).

### Files

- `<session_dir>` = `~/.claude/session-env/<session_id>/` (Claude Code-managed): shim
  dir, session `bazelrc`.
- `/tmp/claude-hd/<session_id>/`: `d.sock` (hook RPC), `daemon.pid`, `daemon.log`
  (incl. session start), `daemon.err.log`, `startup_failure.json` (client startup
  backoff marker).

## Claude Code Telemetry

From Claude Code's native exporter; the Rust daemon emits no OTLP (the profile `otel`
field is ignored).

- **Local machines**: <../../nix/home/claude_code/default.nix> exports directly to
  `https://alloy-otlp.allegedly.works`, bearer via an `otelHeadersHelper` reading the
  SOPS token; NixOS inline Home Manager hosts inherit it via
  <../../nix/home/home.nix>.
- **Web and Haku**: export to `http://127.0.0.1:4318`; <otlp_forwarder.py> (started
  by <ensure_otel_forwarder.sh>) attaches the bearer from
  `DUCKTAPE_OTEL_BEARER_TOKEN` or the mirrored `alloy-otlp-bearer` Secret
  (<../../cluster/k8s/agents/alloy-otlp-bearer/>) and forwards to Alloy.
- **Token**: Authentik `alloy-otlp-client-credentials` provider; the
  `authentik-jwt-rotation` CronJob commits a proxy-scoped JWT SOPS-encrypted to
  `secrets/alloy-otlp-bearer-token.yaml`, which `cli_env.sh`/`web_env.sh` decrypt and
  export. Until the job first succeeds the file is absent and setup warns.

Rationale and hosted-env variables: <plans/transcript_collection.md>. Archive past
cloud sessions with <session_export/README.md>.

## Web Setup

Configure the Claude Code web UI, install mode, telemetry and secret handling using the [web setup guide](docs/web_setup.md).
