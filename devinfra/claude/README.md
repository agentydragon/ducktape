# Claude Code Integration

Session hooks, UDS proxy, statusline, and Claude Code API models for Claude Code
web environments.

## References

- [Claude Code Hooks API](https://docs.anthropic.com/en/docs/claude-code/hooks)
- [Settings JSON Schema](https://json.schemastore.org/claude-code-settings.json)

## Glossary

| Concept                            | Canonical term      | Rationale                                                      |
| ---------------------------------- | ------------------- | -------------------------------------------------------------- |
| Anthropic's Envoy gateway          | **egress proxy**    | Matches Anthropic's own docs ("egress controls"). Unambiguous. |
| mitmproxy testcontainer for tests  | **mitmproxy proxy** | Stock mitmproxy:11 in Docker, simulates egress proxy TLS MITM. |
| "The proxy this proxy forwards to" | **upstream proxy**  | Standard networking term. UDS proxy's upstream = egress proxy. |

## Anthropic's TLS-Inspecting Proxy

Claude Code on the web runs in sandboxed containers with network egress controlled through a TLS-inspecting proxy. Key characteristics:

### Environment Setup (by Anthropic)

Anthropic configures the container environment with:

```bash
HTTPS_PROXY=http://<container_id>:<jwt_token>@<proxy_host>:<port>
HTTP_PROXY=...  # same
```

- **JWT authentication**: Credentials are embedded in the proxy URL as username:password
- **Token refresh**: Anthropic may refresh JWT tokens during long sessions
- **TLS inspection**: Proxy terminates TLS to inspect traffic, re-encrypts with Anthropic CA

### Our Design Principle

**We do NOT overwrite `HTTPS_PROXY` / `HTTP_PROXY` environment variables.**

Most tools (curl, pip, npm, git, etc.) work correctly with Anthropic's proxy. Only Bazel needs special handling due to Java's proxy authentication limitations.

By preserving the original proxy env vars:

- Tools continue to use Anthropic's proxy directly
- JWT token refreshes are automatically picked up
- The bazelisk shim sends fresh credentials to the daemon on each invocation

## Components

- **Session Start Hook**: Sets up the development environment for Claude Code web sessions
- **UDS Proxy**: Routes Bazel gRPC traffic through a Unix domain socket proxy (adds egress proxy auth)

## Session Start Hook

The hook runs at the start of each Claude Code web session and:

### Proxy Setup (via `proxy_setup.py`)

1. UDS proxy starts in-process in the hook daemon for Bazel `--remote_proxy`
2. Loads the TLS inspection CA from the pre-installed filesystem path (`/usr/local/share/ca-certificates/swp-ca-production.crt`)
3. Creates a Java truststore with the CA using keytool
4. Creates combined CA bundle (system CAs + proxy CA)
5. Writes bazelrc to `<session_dir>/bazelrc`

### PATH Shims (via `hook_daemon/shim_install.py`)

7. Bazelisk binary provided by Nix devShell (on PATH)
8. Installs PATH shims at `<session_dir>/bin/{bazelisk,git,bazel,bb,bbr}` — thin
   scripts that report to the hook daemon via `/shim-exec` RPC before exec'ing the
   real binary. The daemon handles proxy credential refresh, `--bazelrc` injection
   (bazelisk), and configurable git safety checks (blocking `git add -A`,
   `git stash`, `git commit --amend` — controlled by `git_shim` in profile config,
   enabled in CLI mode, disabled in web mode). `bazel`, `bb`, `bbr` shims are
   currently no-op passthrough.

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

- **`Setup` hook**: Do NOT register it in `.claude/settings.json`. It would start a daemon
  for the new session ID. The `Setup` hook handler is a noop anyway (the daemon returns
  `{}` immediately).
- **Session-local files** (socket, shim dir, session bazelrc): always keyed by
  `SessionStart`'s session ID, which may be the _old_ ID after a compaction.
- **Session-global files** (bazelisk binary at `~/.cache/claude-hooks/bazelisk`): shared
  across all session IDs, safe for concurrent daemons.

## UDS Proxy for Bazel

Bazel's gRPC remote execution client (gRPC-Java/Netty) cannot reliably authenticate
with HTTP CONNECT proxies due to a timing issue: `ProxyHelper` installs the
`Authenticator` only when a repository rule triggers a download, which may be after
the gRPC channel is already established. The UDS proxy bypasses this entirely —
Bazel's `--remote_proxy=unix:<path>` routes gRPC traffic through the UDS natively.

```
Most tools (curl, pip, npm, etc.)
    └──► HTTPS_PROXY (Anthropic's proxy) ──► Internet
         (unchanged, fresh JWT)

Bazel gRPC (remote execution, cache, BES)
    └──► --remote_proxy=unix:<session_dir>/remote-proxy.sock
           └──► UdsRemoteProxy (in hook daemon)
                  └──► CONNECT tunnel through egress proxy ──► remote.buildbuddy.io
```

BCR fetches use native JVM proxy settings from Anthropic's `JAVA_TOOL_OPTIONS`.

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

CA/truststore files (in `<session_dir>/auth-proxy/`, created by `proxy_setup.py`):

- `anthropic_ca.pem` - Loaded TLS inspection CA
- `combined_ca.pem` - System CAs + Anthropic CA bundle
- `cacerts.jks` - Java truststore with CA

Global (non-session-scoped) files in `~/.cache/claude-hooks/`:

- `bazelisk` - Bazelisk binary
- `mkcert` - mkcert binary

## Known Limitations

### 9p filesystem doesn't support Unix socket hard links

**Affects**: Claude Code web gVisor sandbox (root `/` is 9p)

**Root cause**: Supervisord uses hard links for atomic Unix socket creation (`link()` syscall). The 9p filesystem doesn't support hard linking Unix domain sockets, returning `EOPNOTSUPP` (errno 95). When the hard link fails, supervisord misinterprets this as a stale socket and enters an infinite retry loop.

**Solution**: Use TCP socket (`inet_http_server`) instead of Unix socket. The supervisor_setup module now configures supervisor to listen on `127.0.0.1:19001` by default. This avoids the 9p filesystem limitation entirely.

Configuration via environment variable:

- `DUCKTAPE_CLAUDE_HOOKS_SUPERVISOR_PORT`: Override TCP port (default: 19001)

## Test Environments

### Proxy in Tests

Tests that need an egress proxy simulation use a **mitmproxy testcontainer** (stock
`mitmproxy:11` in Docker). The fixture generates a mock CA, starts mitmdump with Basic
auth, and exposes the proxy on a random host port. Tests point `HTTPS_PROXY` at it.

The mitmproxy container connects directly to the internet — no upstream proxy chaining.
Tests requiring a proxy are designed to run on RBE or CI where direct internet access
is available. They are **not** compatible with Claude Code web's egress proxy.

## OTEL Tracing

Hooks emit OpenTelemetry traces to Grafana Alloy via Authentik proxy at
`alloy-otlp.allegedly.works`. Bearer token: web — decrypted via `startup_env_script`
(`web_env.sh`) at daemon startup; CLI — sourced via `.envrc` (`cli_env.sh`).

Configured in the profile path (`otel.endpoint`, `secrets.otel_bearer_token`).

Key files: TF module in `cluster/terraform/gitops/alloy-otlp-bearer-token/`,
Authentik blueprint in `cluster/k8s/authentik/app/blueprints/alloy-otlp-sso.yaml`.
Rotation: bump `rotation_version` in the TF module.

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
2. Writes decrypted secrets into `.claude/settings.local.json` for MCP servers
3. Skills symlinked into `~/.claude/skills/` (preserves Anthropic defaults)

Secrets (`GITHUB_TOKEN`, `BUILDBUDDY_API_KEY`, `DUCKTAPE_OTEL_BEARER_TOKEN`, etc.) are
decrypted in **two places** because different consumers read from different sources:

- **`web_setup.sh`** → `settings.local.json`: the only path that reaches **MCP server**
  processes. Claude Code injects `settings.local.json["env"]` into MCP servers directly.
- **Hook daemon `startup_env_script`** → session env file: the path for **hook
  subprocesses**, which source the session env file written by `SessionStart`.

The kube MCP server (`claude-sandbox-kubectl`) is special: it self-decrypts via
`kube_from_sops.sh` at startup and does not read from `settings.local.json`.

See <docs/secrets_env_flow.md> for the full picture.
