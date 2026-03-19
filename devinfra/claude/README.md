# Claude Code Integration

Session hooks, auth proxy, statusline, and Claude Code API models for Claude Code
web environments.

## References

- [Claude Code Hooks API](https://docs.anthropic.com/en/docs/claude-code/hooks)
- [Settings JSON Schema](https://json.schemastore.org/claude-code-settings.json)

## Glossary

| Concept                            | Canonical term        | Rationale                                                                                                                         |
| ---------------------------------- | --------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| Anthropic's Envoy gateway          | **egress proxy**      | Matches Anthropic's own docs ("egress controls"). Unambiguous.                                                                    |
| Local auth-adding proxy            | **auth proxy**        | Describes function. Short.                                                                                                        |
| Mock TLS MITM for tests            | **mock egress proxy** | Says what it simulates.                                                                                                           |
| "The proxy this proxy forwards to" | **upstream proxy**    | Standard networking term. Context-dependent (auth proxy's upstream = egress proxy; mock's upstream = auth proxy or egress proxy). |

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

1. Auth proxy starts in-process in the hook daemon at daemon startup (daemon threads, port `127.0.0.1:18081`)
2. Session start writes credentials to the daemon's creds file
3. Loads the TLS inspection CA from the pre-installed filesystem path (`/usr/local/share/ca-certificates/swp-ca-production.crt`)
4. Creates a Java truststore with the CA using keytool
5. Creates combined CA bundle (system CAs + proxy CA)
6. Writes bazelrc to `<session_dir>/auth-proxy/bazelrc`

### Bazel Setup (via `bazelisk_setup.py`)

7. Downloads and installs Bazelisk
8. Creates wrapper script at `<session_dir>/bin/bazel`

### Git Hooks

9. Installs git pre-commit hooks via pre-commit framework

### Development Tools

Note: flux, kustomize, kubeseal, helm are now Bazel-managed via `@multitool//tools/*`.
Nix formatting uses a static nixfmt binary downloaded by `devinfra/precommit/run_nixfmt.sh`.

### Environment Configuration

12. Configures podman for gVisor compatibility
13. Sets up environment variables in `CLAUDE_ENV_FILE`

See `.claude/settings.json` for hook configuration.

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

- Runs on `localhost:18081`, accepts **unauthenticated** CONNECT requests
- Bazel's JVM is configured with `-Dhttps.proxyHost=127.0.0.1 -Dhttps.proxyPort=18081`
- gRPC-Java's `ProxyDetectorImpl` sees the proxy via `ProxySelector` and connects without needing credentials
- The auth proxy injects `Proxy-Authorization: Basic` credentials when forwarding to the egress proxy
- Reads credentials from a file on each connection, enabling JWT hot-reload without restart

## References

See <proxy-alternatives.md> for analysis of why alternatives don't work.

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
| `DUCKTAPE_CLAUDE_HOOKS_INSTALL_BAZELISK`  | `true`                     | Install bazelisk                                  |
| `DUCKTAPE_CLAUDE_HOOKS_CONTAINER_RUNTIME` | `docker`                   | Container runtime (`podman`, `docker`, or `none`) |

`<session_dir>` = `~/.claude/session-env/<session_id>/` — a per-session directory managed by Claude Code.

See `settings.py` for the full configuration schema.

## Dependencies

See BUILD.bazel for the full dependency list. Key runtime requirements:

- **keytool** (from JDK) for Java truststore creation

## Usage

The proxy runs in-process within the hook daemon as daemon threads. It starts
automatically when the daemon starts (if `HTTPS_PROXY` is set) and stops when
the daemon shuts down. Credentials are written by session start and read on
each connection (hot-reload).

## How It Works

### Proxy Architecture

```
Most tools (curl, pip, npm, etc.)
    │
    └──► HTTPS_PROXY (Anthropic's proxy) ──► Internet
         (unchanged, fresh JWT)

Bazel/Bazelisk
    │
    └──► bazel wrapper
           │
           ├── 1. Reads HTTPS_PROXY (fresh JWT from Anthropic)
           ├── 2. Writes to creds file (<session_dir>/hook-daemon/upstream_proxy)
           ├── 3. Sets HTTPS_PROXY=localhost:18081 for subprocess only
           └── 4. Execs bazelisk
                   │
                   └──► Auth proxy (localhost:18081)
                          │
                          ├── Reads creds file on each connection
                          ├── Adds Proxy-Authorization header
                          └──► Anthropic's proxy ──► Internet
```

### Flow Details

1. **Hook daemon** starts the auth proxy in-process at daemon startup
2. **Bazel wrapper** (invoked instead of bazel directly):
   - Reads current `HTTPS_PROXY` from environment (Anthropic's proxy with fresh JWT)
   - Writes upstream URL to credentials file (for the long-running proxy daemon)
   - Sets `HTTPS_PROXY=localhost:18081` for the bazel subprocess only
   - Execs bazelisk with proxy configuration
3. **Auth proxy** (long-running daemon):
   - Reads credentials file on each connection (picks up fresh JWT)
   - Forwards CONNECT requests to Anthropic's proxy with auth header

### Why This Design

- **Fresh credentials**: Bazel wrapper reads `HTTPS_PROXY` on each invocation, so JWT refreshes are picked up
- **No global override**: Other tools continue to use Anthropic's proxy directly
- **Hot-reload**: Auth proxy reads creds file per-connection, enabling credential updates without restart

## Lifecycle Management

The auth proxy runs in-process within the hook daemon:

- **Host process**: Hook daemon (FastAPI on UDS, `~/.claude/session-env/<session_id>/hook-daemon/`)
- **Threading**: Daemon threads (`ThreadPoolExecutor`), non-blocking `start()`/`stop()`
- **Shutdown**: Stopped automatically when the hook daemon shuts down (idle timeout or SIGTERM)
- **Credentials**: Read from file on each connection (hot-reload)
- **Creds file**: `~/.claude/session-env/<session_id>/hook-daemon/upstream_proxy`

## Verification

After session start:

```bash
# Proxy should be accessible
curl -s --max-time 5 -x http://127.0.0.1:18081 https://bcr.bazel.build/ | head -1

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

- `upstream_proxy` - Auth proxy credentials (read by in-process proxy on each connection)
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

### How Tests Work in Each Environment

**GitHub Actions CI** (no egress proxy):

- `HTTPS_PROXY` is not set
- `MockEgressProxy` connects directly to the internet
- The auth proxy is started by the test's session start hook but never receives traffic
  (nothing points `HTTPS_PROXY` at it — the mock connects directly)
- DNS resolution works directly

**Claude Code Web** (gVisor sandbox with egress proxy):

- `HTTPS_PROXY` is set to `http://CONTAINER:JWT@host:port` by Anthropic
- The bazel wrapper rewrites `HTTPS_PROXY=http://localhost:18081` before exec'ing bazelisk
- `env_inherit` in BUILD.bazel passes the **rewritten** `HTTPS_PROXY` to the test process
- `MockEgressProxy` detects `HTTPS_PROXY=localhost:18081` via `EgressProxyConfig.from_env()`
  and chains through: mock → auth proxy (18081) → egress proxy → internet
- DNS does NOT work directly (all traffic must go through egress proxy)

**Developer laptop** (no proxy):

- Same as CI — `MockEgressProxy` connects directly

### Proxy Chain in Tests (Claude Code Web)

```
test client (e.g. bazel, podman)
    │
    └──► mock egress proxy (random port, TLS MITM)
           │ simulates Anthropic's TLS inspection
           │ chains through HTTPS_PROXY if set
           └──► auth proxy (localhost:18081, no TLS)
                  │ adds Proxy-Authorization: Basic
                  └──► egress proxy (21.x.x.x:15004)
                         │ TLS inspection, JWT validation
                         └──► internet
```

### The `env_inherit` + Bazel Wrapper Interaction

The BUILD.bazel target has `env_inherit = ["HTTPS_PROXY", ...]`. When tests run via the `bazel` command (which is actually the bazel wrapper), the wrapper rewrites `HTTPS_PROXY` to `localhost:18081` before exec'ing bazelisk. So the test process inherits the rewritten value, not the original egress proxy URL.

This is correct behavior: it means the mock egress proxy chains through the auth proxy, which adds credentials and forwards to the real egress proxy. The full chain works.

## Encrypted Secrets

Secrets are stored as per-component age-encrypted JSON files in `.claude_hooks/secrets/` at the repo root — NOT inside the wheel. Each `.age` file decrypts to a JSON dict mapping env var names to values (e.g. `{"OLLAMA_API_KEY": "..."}"`).

When `DUCKTAPE_CLAUDE_HOOKS_SECRETS_AGE_KEY` is set (in Claude Code web environment), the session start hook decrypts all `.age` files and exports the merged env vars to `CLAUDE_ENV_FILE`.

Uses asymmetric X25519 encryption via [age](https://age-encryption.org/):

- **Public key**: `.claude_hooks/secrets/recipients.txt` (anyone can encrypt)
- **Private key**: `DUCKTAPE_CLAUDE_HOOKS_SECRETS_AGE_KEY` env var (only the decryptor needs this)
- **Repo-specific context**: `.claude_hooks/templates/context.mako` is rendered and appended to the session context

### Decrypting a component

```bash
age -d -i <key_file> .claude_hooks/secrets/ollama.age
```

### Editing a component

```bash
# Decrypt to a temp file
age -d -i <key_file> .claude_hooks/secrets/ollama.age > /tmp/component.json

# Edit the JSON dict
$EDITOR /tmp/component.json

# Re-encrypt
age -e -R .claude_hooks/secrets/recipients.txt /tmp/component.json > .claude_hooks/secrets/ollama.age

# Clean up
rm /tmp/component.json
```

### Adding a new recipient

Add their age public key (one per line) to `.claude_hooks/secrets/recipients.txt`, then re-encrypt all component files.

## OTEL Tracing

Hooks emit OpenTelemetry traces to Grafana Alloy via Authentik proxy at
`alloy-otlp.allegedly.works`. Fully declarative — token flows through
Terraform → Vault → ESO → k8s secrets → `otel.py`.

Configured in `.claude_hooks/config.yaml` (`otel.endpoint`). Bearer token
loaded from k8s secret (`k8s_secrets.otel_bearer_token`).

Key files: TF module in `cluster/terraform/gitops/alloy-otlp-bearer-token/`,
Authentik blueprint in `cluster/k8s/authentik/blueprints/alloy-otlp-sso.yaml`.
Rotation: bump `rotation_version` in the TF module.

## Development

```bash
# Run tests
bazel test //devinfra/claude:test_proxy
```
