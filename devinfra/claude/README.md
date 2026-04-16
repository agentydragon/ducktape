# Claude Code Integration

Session hooks, statusline, and Claude Code API models for Claude Code
web environments.

## References

- [Claude Code Hooks API](https://docs.anthropic.com/en/docs/claude-code/hooks)
- [Settings JSON Schema](https://json.schemastore.org/claude-code-settings.json)

## Networking

Current Claude Code web containers expose direct internet via a **transparent
TLS-inspecting proxy** at the network layer — no `HTTPS_PROXY` env vars are
set, and Anthropic's inspection CA is pre-installed into the system CA bundle
(`/etc/ssl/certs/ca-certificates.crt`). All tools — curl, Bazel, pip, npm,
kubectl, git — work without per-tool proxy configuration.

The SessionStart hook runs a quick probe (`session_start/connectivity.py`)
and emits a WARNING in the session banner if direct internet fails. If that
warning starts appearing on new sessions, the container generation has
likely changed back to requiring explicit proxy env vars — see the
[historical note](#historical-explicit-egress-proxy) below.

## Specification

See <hook_daemon/SPEC.md> for the high-level, user-facing specification of
what the hook daemon guarantees to every Claude Code session (on CLI and on
web). Read that first if you want to know **what** the daemon does for the
agent — this README covers **how** those behaviors are implemented.

## Components

- **Session Start Hook**: Sets up the development environment for Claude Code web sessions.

## Session Start Hook

The hook runs at the start of each Claude Code web session and:

### Connectivity Probe (via `session_start/connectivity.py`)

Verifies direct internet reachability to BuildBuddy. Emits a WARNING to
the session banner on failure.

### PATH Shims (via `hook_daemon/shim_install.py`)

7. Bazelisk binary provided by Nix devShell (on PATH)
8. Installs PATH shims at `<session_dir>/bin/{bazelisk,git,bazel,bb,bbr}` — two-line
   shell scripts that `exec claude-hook shim <name>` (PATH-resolved at invocation
   time), which reports to the hook daemon via `/shim-exec` RPC before exec'ing the
   real binary. The daemon handles proxy credential refresh, `--bazelrc` injection
   (bazelisk), and configurable git safety checks (blocking `git add -A`,
   `git stash`, `git commit --amend` — controlled by `git_shim` in profile config,
   enabled in CLI mode, disabled in web mode). `bazel`, `bb`, `bbr` shims are
   currently no-op passthrough.

   Because `claude-hook` is resolved via PATH at exec time (not baked as a store
   path at install time), `nix profile install` / `home-manager switch` takes
   effect for all subsequent shim invocations without restarting the session.

### Git Hooks

9. Installs git pre-commit hooks via pre-commit framework

### Development Tools

Note: flux, kustomize, helm are now Bazel-managed via `@multitool//tools/*`.
Nix formatting uses `nixfmt` from the Nix devShell.

### Environment Configuration

12. Sets up environment variables in `CLAUDE_ENV_FILE`

See `.claude/settings.json` for hook configuration.

## Observed: `Setup` and `SessionStart` Use Different Session IDs

**Observed 2026-03-21 during session compaction.** Claude Code sends hook events with
_mismatched_ session IDs: the `Setup` hook fires with the **new** post-compaction session
ID, while the `SessionStart` hook fires with the **old** pre-compaction session ID (with
`source: compact`).

Example (from daemon traces):

| Hook           | Session ID                                   |
| -------------- | -------------------------------------------- |
| `Setup`        | `f1126fbf-c415-48e0-8b16-09b95c4b556a` (new) |
| `SessionStart` | `c11a6aa8-4bb3-4bfb-8d25-3224a2ab7efb` (old) |

**Why this matters — session-local vs session-global state:**

The hook daemon is keyed by session ID: each session ID gets its own socket path and daemon
directory. When `Setup` starts a daemon for the new ID, and `SessionStart` arrives for the
old ID, the client finds no socket for the old ID and tries to start a _second_ daemon.

**Consequences:**

- **`Setup` hook with `claude-hook`**: Do NOT register `claude-hook` for the `Setup`
  event. It would start a daemon for the new session ID. The `Setup` hook handler is a
  noop anyway (the daemon returns `{}` immediately).
- **`Setup` hook with plain shell scripts**: Safe, as long as the script does NOT call
  `claude-hook`. We register `bash devinfra/claude/web_setup_hook.sh` for Setup — it
  reinstalls devtools before SessionStart fires, but never invokes `claude-hook`.
- **Session-local files** (socket, shim dir, session bazelrc): always keyed by
  `SessionStart`'s session ID, which may be the _old_ ID after a compaction.
- **Session-global files** (bazelisk binary at `~/.cache/claude-hooks/bazelisk`): shared
  across all session IDs, safe for concurrent daemons.

## Historical: Explicit Egress Proxy

Earlier Claude Code web containers injected `HTTPS_PROXY=http://<container_id>:<jwt>@<host>:<port>`
into the process environment. The hook daemon previously ran a substantial
subsystem under `devinfra/claude/auth_proxy/` to handle it:

- Load the TLS inspection CA from `/usr/local/share/ca-certificates/swp-ca-production.crt`
- Build a Java truststore (`cacerts.jks`) for Bazel JVM's HTTPS
- Build a combined CA bundle (`combined_ca.pem`) for `SSL_CERT_FILE`, `CURL_CA_BUNDLE`, etc.
- Start a UDS proxy (`UdsRemoteProxy`) that Bazel used via `--remote_proxy=unix:<path>`
  to work around gRPC-Java's `Authenticator` installation timing bug with HTTP
  CONNECT proxies.
- A BES interceptor that inspected Bazel build events and forwarded them to BuildBuddy.

Current containers handle proxying transparently at the network layer with
the Anthropic CA in the system CA bundle, so none of the above is needed —
every tool just works. The entire `auth_proxy/` subsystem was removed; see
git log for the removal. If Anthropic reverts to explicit proxy env vars,
restore the subsystem from history.

## Configuration

| Environment Variable                    | Default | Description          |
| --------------------------------------- | ------- | -------------------- |
| `DUCKTAPE_CLAUDE_HOOKS_SUPERVISOR_PORT` | `19001` | Supervisor TCP port  |
| `DUCKTAPE_CLAUDE_HOOKS_PROFILE`         | (none)  | Path to profile YAML |

`<session_dir>` = `~/.claude/session-env/<session_id>/` — a per-session directory managed by Claude Code.

See `settings.py` for the full configuration schema.

## References

- [Claude Code on the Web](https://www.anthropic.com/news/claude-code-on-the-web) - Product announcement
- [Claude Code Sandboxing](https://www.anthropic.com/engineering/claude-code-sandboxing) - Network isolation architecture
- [Enterprise Network Configuration](https://docs.anthropic.com/en/docs/claude-code/corporate-proxy) - Proxy and CA configuration
- [Network Security](https://docs.anthropic.com/en/docs/claude-code/security#network-access) - Egress controls

## Files

All session-scoped files live under `<session_dir>` = `~/.claude/session-env/<session_id>/`.

Supervisor files (in `<session_dir>/supervisor/`, used for container runtime only):

- `supervisord.conf` - Supervisor main configuration
- `supervisord.{log,pid}` - Supervisor daemon state

Note: Supervisor listens on TCP `127.0.0.1:19001` (no Unix socket file).

Hook daemon files (in `<session_dir>/hook-daemon/`):

- `daemon.sock` - UDS for hook RPC
- `daemon.pid` - Daemon pidfile
- `daemon.log` - Daemon and session start logs

Global (non-session-scoped) files in `~/.cache/claude-hooks/`:

- `bazelisk` - Bazelisk binary

## Known Limitations

### 9p filesystem doesn't support Unix socket hard links

**Affects**: Claude Code web gVisor sandbox (root `/` is 9p)

**Root cause**: Supervisord uses hard links for atomic Unix socket creation (`link()` syscall). The 9p filesystem doesn't support hard linking Unix domain sockets, returning `EOPNOTSUPP` (errno 95). When the hard link fails, supervisord misinterprets this as a stale socket and enters an infinite retry loop.

**Solution**: Use TCP socket (`inet_http_server`) instead of Unix socket. The supervisor_setup module now configures supervisor to listen on `127.0.0.1:19001` by default. This avoids the 9p filesystem limitation entirely.

Configuration via environment variable:

- `DUCKTAPE_CLAUDE_HOOKS_SUPERVISOR_PORT`: Override TCP port (default: 19001)

## OTEL Tracing

Hooks emit OpenTelemetry traces to Grafana Alloy via Authentik proxy at
`alloy-otlp.allegedly.works`. Authentik is the canonical source for the bearer
token: the TF module creates an Authentik service account + token and writes
the generated value into the `alloy-otlp-bearer-token` Secret in
`claude-sandbox`. `cli_env.sh` and `web_env.sh` read that Secret via `kubectl`
and export it as `DUCKTAPE_OTEL_BEARER_TOKEN`.

Configured in the profile path (`otel.endpoint`, `secrets.otel_bearer_token`).

Key files: TF module in <cluster/terraform/gitops/alloy-otlp-bearer-token/>
(provisions proxy provider, application, service account, token, policy
binding, and the K8s Secret). Rotation: `tofu taint
authentik_token.alloy_otlp` and let tofu-controller reconcile, or delete the
Authentik token via the UI and wait for the next TF apply.

## Web Setup

To use this repository with Claude Code on the web, configure **both** of the following in the Claude Code web UI:

### 1. Environment Variables (Claude Code web UI → Settings → Environment Variables)

These must be configured as env vars in the Claude Code web UI so they are injected into the Claude process at startup:

| Variable                        | Description                                                                  |
| ------------------------------- | ---------------------------------------------------------------------------- |
| `DUCKTAPE_CLAUDE_HOOKS_PROFILE` | Path to the profile: `devinfra/claude/hook_daemon/profiles/web/profile.yaml` |
| `SOPS_AGE_KEY`                  | Age private key for SOPS decryption (format: `AGE-SECRET-KEY-1...`)          |

`DUCKTAPE_CLAUDE_HOOKS_PROFILE` is needed so Claude Code injects the profile path into all hook subprocesses.
`SOPS_AGE_KEY` is the age private key for decrypting secrets. The hook daemon receives it from the Claude process environment via `startup_env_script`.

### 2. Setup Script

```bash
bash ducktape/devinfra/claude/web_setup.sh
```

This runs <web_setup.sh> which installs:

1. Nix + devtools (`claude-hooks` wheel, `bbapi`, `gh`, `sops`, skills)
2. `github-no-proxy` git remote + `buildbuddy.remote-bazel-remote-name` for bbr
3. Skills symlinked into `~/.claude/skills/` (preserves Anthropic defaults)

Secrets are **not** decrypted by `web_setup.sh`. `SOPS_AGE_KEY` is a user UI env var
delivered only to the interactive Claude Code process — not to the setup script. All
decryption happens in the hook daemon via `startup_env_script` (`web_env.sh`) at
daemon startup, once `SOPS_AGE_KEY` is available in the inherited env.

See <docs/secrets_env_flow.md> for the full picture.
