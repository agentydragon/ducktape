# Claude Code Integration

Session hooks, auth proxy, statusline, and Claude Code API models for Claude Code
web environments.

## References

- [Claude Code Hooks API](https://docs.anthropic.com/en/docs/claude-code/hooks)
- [Settings JSON Schema](https://json.schemastore.org/claude-code-settings.json)

## Glossary

| Concept                            | Canonical term      | Rationale                                                       |
| ---------------------------------- | ------------------- | --------------------------------------------------------------- |
| Anthropic's Envoy gateway          | **egress proxy**    | Matches Anthropic's own docs ("egress controls"). Unambiguous.  |
| Local auth-adding proxy            | **auth proxy**      | Describes function. Short.                                      |
| mitmproxy testcontainer for tests  | **mitmproxy proxy** | Stock mitmproxy:11 in Docker, simulates egress proxy TLS MITM.  |
| "The proxy this proxy forwards to" | **upstream proxy**  | Standard networking term. Auth proxy's upstream = egress proxy. |

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
- The bazel wrapper reads fresh credentials on each invocation

## Components

- **Session Start Hook**: Sets up the development environment for Claude Code web sessions
- **Auth Proxy**: Adds authentication headers for Bazel's proxy connections (not global)

## Session Start Hook

The hook runs at the start of each Claude Code web session and:

### Proxy Setup (via `proxy_setup.py`)

1. Auth proxy starts in-process in the hook daemon (daemon threads, OS-assigned port) on the first `SessionStart`
2. Session start sets credentials on the in-process proxy via `proxy.set_creds()`
3. Loads the TLS inspection CA from the pre-installed filesystem path (`/usr/local/share/ca-certificates/swp-ca-production.crt`)
4. Creates a Java truststore with the CA using keytool
5. Creates combined CA bundle (system CAs + proxy CA)
6. Writes bazelrc to `<session_dir>/bazelrc`

### Bazel Setup (via `hook_daemon/session_start/bazelisk.py`)

7. Bazelisk binary provided by Nix web-session package (on PATH)
8. Creates wrapper script at `<session_dir>/bin/bazel` (injects proxy credentials)

### Git Hooks

9. Installs git pre-commit hooks via pre-commit framework

### Development Tools

Note: flux, kustomize, kubeseal, helm are now Bazel-managed via `@multitool//tools/*`.
Nix formatting uses `nixfmt` from the Nix devShell / web-session package.

### Environment Configuration

12. Configures podman for gVisor compatibility
13. Sets up environment variables in `CLAUDE_ENV_FILE`

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
- **Session-local files** (socket, wrapper dir, session bazelrc): always keyed by
  `SessionStart`'s session ID, which may be the _old_ ID after a compaction.
- **Session-global files** (bazelisk binary at `~/.cache/claude-hooks/bazelisk`): shared
  across all session IDs, safe for concurrent daemons.

# Auth Proxy

A local HTTP CONNECT proxy that adds authentication to Anthropic's egress proxy, enabling Bazel's gRPC remote execution and BCR access.

## Why This Exists

[Claude Code on the web](https://docs.anthropic.com/en/docs/claude-code/claude-code-on-the-web) runs in ephemeral containers with a TLS-inspecting proxy for network egress. Bazel needs to route both **BCR fetches** (Java HTTP) and **gRPC remote execution** (gRPC-Java/Netty) through this proxy.

### The Problem: gRPC-Java Cannot Authenticate with the Egress Proxy

Bazel's Java HTTP layer _can_ authenticate with the egress proxy: `ProxyHelper` reads `HTTPS_PROXY`, installs a `java.net.Authenticator`, and handles 407 challenges. BCR fetches work this way. However, **gRPC-Java's remote execution client cannot reliably authenticate**:

1. **gRPC-Java's `ProxyDetectorImpl`** reads proxy settings from `ProxySelector.getDefault()` (which uses JVM system properties `-Dhttps.proxyHost`/`-Dhttps.proxyPort`), then calls `Authenticator.requestPasswordAuthentication()` for credentials
2. **Bazel's `ProxyHelper`** installs the `Authenticator` — but only when a repository rule triggers a download. The gRPC remote execution channel may already be established before any repository rule runs
3. **Timing dependency**: If gRPC creates its channel before `ProxyHelper` installs the `Authenticator`, the `ProxyDetectorImpl` gets no credentials and gRPC's Netty transport sends an unauthenticated CONNECT → the egress proxy returns 407 → connection fails with "Unable to resolve host"

### The Solution: Local Unauthenticated Proxy

The auth proxy decouples authentication from this timing problem:

- Runs on `localhost:<port>` (OS-assigned), accepts **unauthenticated** CONNECT requests
- Bazel's JVM is configured with `-Dhttps.proxyHost=127.0.0.1 -Dhttps.proxyPort=<port>`
- gRPC-Java's `ProxyDetectorImpl` sees the proxy via `ProxySelector` and connects without needing credentials
- The auth proxy injects `Proxy-Authorization: Basic` credentials when forwarding to the egress proxy
- Credentials are held in-memory; `bazel_wrapper` updates them via RPC before each invocation

## References

See <docs/proxy_alternatives.md> for analysis of why alternatives don't work.

- [Claude Code on the Web](https://www.anthropic.com/news/claude-code-on-the-web) - Product announcement
- [Claude Code Sandboxing](https://www.anthropic.com/engineering/claude-code-sandboxing) - Network isolation architecture
- [Enterprise Network Configuration](https://docs.anthropic.com/en/docs/claude-code/corporate-proxy) - Proxy and CA configuration
- [Network Security](https://docs.anthropic.com/en/docs/claude-code/security#network-access) - Egress controls

## Configuration

All settings use [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) with the `DUCKTAPE_CLAUDE_HOOKS_` prefix:

| Environment Variable                      | Default                    | Description                                       |
| ----------------------------------------- | -------------------------- | ------------------------------------------------- |
| `DUCKTAPE_CLAUDE_HOOKS_SUPERVISOR_DIR`    | `<session_dir>/supervisor` | Supervisor config directory                       |
| `DUCKTAPE_CLAUDE_HOOKS_SUPERVISOR_PORT`   | `19001`                    | Supervisor TCP port                               |
| `DUCKTAPE_CLAUDE_HOOKS_AUTH_PROXY_DIR`    | `<session_dir>/auth-proxy` | Proxy cache directory                             |
| `DUCKTAPE_CLAUDE_HOOKS_AUTH_PROXY_PORT`   | `18081`                    | Auth proxy port                                   |
| `DUCKTAPE_CLAUDE_HOOKS_CONTAINER_RUNTIME` | `docker`                   | Container runtime (`podman`, `docker`, or `none`) |

`<session_dir>` = `~/.claude/session-env/<session_id>/` — a per-session directory managed by Claude Code.

See `settings.py` for the full configuration schema.

## Dependencies

See BUILD.bazel for the full dependency list. Key runtime requirements:

- **keytool** (from JDK) for Java truststore creation

## How It Works

```
Most tools (curl, pip, npm, etc.)
    └──► HTTPS_PROXY (Anthropic's proxy) ──► Internet
         (unchanged, fresh JWT)

Bazel/Bazelisk
    └──► bazel wrapper
           ├── 1. Reads HTTPS_PROXY (fresh JWT from Anthropic)
           ├── 2. RPC to hook daemon: POST /update-proxy-creds → in-memory update
           ├── 3. Sets HTTPS_PROXY=localhost:<port> for subprocess only
           └── 4. Execs bazelisk
                   └──► Auth proxy (localhost:<port>)
                          ├── Reads in-memory credentials (set via RPC)
                          ├── Adds Proxy-Authorization header
                          └──► Anthropic's proxy ──► Internet
```

The auth proxy runs in-process within the hook daemon (daemon threads, FastAPI on UDS).
Credentials are held in-memory on the proxy object and updated via RPC before each bazel
invocation. Proxy is started lazily on `SessionStart`, stopped on daemon shutdown.

## Verification

After session start:

```bash
# Proxy should be accessible (AUTH_PROXY_URL is set in the session env file)
curl -s --max-time 5 -x "$AUTH_PROXY_URL" https://bcr.bazel.build/ | head -1

# Bazel should be able to access BCR
bazel info

# Check hook daemon logs (proxy runs in-process)
tail -20 "$DUCKTAPE_CLAUDE_HOOKS_SESSION_DIR/hook-daemon/daemon.log"
```

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

Auth proxy files (in `<session_dir>/auth-proxy/`, created by `proxy_setup.py`):

- `anthropic_ca.pem` - Loaded TLS inspection CA
- `combined_ca.pem` - System CAs + Anthropic CA bundle
- `cacerts.jks` - Java truststore with CA

Global (non-session-scoped) files in `~/.cache/claude-hooks/`:

- `bazelisk` - Bazelisk binary
- `mkcert` - mkcert binary

## Known Limitations

### rules_python lock() doesn't inherit --action_env

The `lock()` rule from `@rules_python//python/uv:lock.bzl` has a bug/limitation: it doesn't inherit `--action_env` values because it sets an explicit `env` attribute on `ctx.actions.run_shell()`.

**Impact**: The `uv pip compile` sandbox action doesn't receive proxy environment variables set via `--action_env=HTTPS_PROXY=...`.

**Workaround**: Pass proxy env vars directly to the `lock()` rule's `env` attribute:

```starlark
lock(
    name = "requirements",
    srcs = [...],
    out = "requirements_bazel.txt",
    env = {
        "HTTPS_PROXY": "http://localhost:18081",
        "SSL_CERT_FILE": "/path/to/combined_ca.pem",  # For TLS inspection
    },
)
```

**Root cause**: In `python/uv/private/lock.bzl`:

```starlark
ctx.actions.run_shell(
    ...
    env = ctx.attr.env,  # <-- Explicit env overrides --action_env inheritance
)
```

This should arguably use `dicts.add(ctx.configuration.default_shell_env, ctx.attr.env)` to merge `--action_env` with rule-specific env.

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
`alloy-otlp.allegedly.works`. Fully declarative — token flows through
Terraform → Vault → ESO → k8s secrets → `otel.py`.

Configured in `.claude_hooks/config.yaml` (`otel.endpoint`). Bearer token
loaded from k8s secret (`k8s_secrets.otel_bearer_token`).

Key files: TF module in `cluster/terraform/gitops/alloy-otlp-bearer-token/`,
Authentik blueprint in `cluster/k8s/authentik/app/blueprints/alloy-otlp-sso.yaml`.
Rotation: bump `rotation_version` in the TF module.

## Web Setup

To use this repository with Claude Code on the web, configure the following setup script in the Claude Code web UI:

```bash
#!/bin/bash
curl -fsSL https://raw.githubusercontent.com/agentydragon/ducktape/680d78946bf72e5e3601cdb69299546b495cab1b/devinfra/claude/web_setup.sh | bash
```

**Note**: Use a pinned commit SHA, not the `devel` branch ref — Anthropic caches
the setup script at configuration time (when saved in the web UI). Changing the
URL triggers a re-fetch; updating `devel` alone does not. Update the SHA when
`web_setup.sh` changes (see <docs/web-setup-debug.md> for history).

This runs <web_setup.sh> which installs:

1. The `claude-hooks` wheel (provides the `claude-hook` binary used by hooks in `.claude/settings.json`)
2. Skills tarball (deployed to `~/.claude/skills/` for AI agent use)
