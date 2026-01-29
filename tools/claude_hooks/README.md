# Claude Web Hooks

Session hooks and auth proxy for Claude Code web environments.

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

1. Starts supervisord for process management
2. Registers proxy with supervisor at `127.0.0.1:18081`
3. Extracts the TLS inspection CA from the proxy via Python 3.13+ ssl APIs
4. Creates a Java truststore with the CA using keytool
5. Creates combined CA bundle (system CAs + proxy CA)
6. Writes bazelrc to `~/.cache/claude-hooks/auth-proxy/bazelrc`

### Bazel Setup (via `bazelisk_setup.py`)

7. Downloads and installs Bazelisk
8. Creates wrapper script at `~/.cache/claude-hooks/auth-proxy/bin/bazel`

### Git Hooks

9. Installs git pre-commit hooks via pre-commit framework

### Development Tools

10. Installs nix via `nix_setup.py` (for nix eval, flake operations, nixfmt)

Note: flux, kustomize, kubeseal, helm are now Bazel-managed via `@multitool//tools/*`.
Nix formatting uses `nix run nixpkgs#nixfmt` via the NixOS/nixfmt pre-commit hook.

### Environment Configuration

12. Configures podman for gVisor compatibility
13. Sets up environment variables in `CLAUDE_ENV_FILE`

See `.claude/settings.json` for hook configuration.

# Auth Proxy

An auth proxy that adds authentication headers for upstream TLS-inspecting proxies, enabling Bazel to access the Bazel Central Registry (BCR).

## Why This Exists

[Claude Code on the web](https://docs.anthropic.com/en/docs/claude-code/claude-code-on-the-web) runs in ephemeral containers with a TLS-inspecting proxy for network egress. This breaks Bazel's access to BCR due to multiple Java/JVM limitations:

### The Problem

1. **TLS Inspection**: The proxy does TLS inspection with a custom Anthropic CA certificate
2. **JWT Authentication**: Proxy credentials include a JWT token for authentication (see [network docs](https://docs.anthropic.com/en/docs/claude-code/security#network-access))
3. **Java doesn't read env vars**: Standard Java networking uses system properties (`https.proxyHost`), not `HTTPS_PROXY` environment variables
4. **HTTP 401 vs 407**: Java's `Authenticator` class only triggers on HTTP 407 (Proxy Authentication Required), but Claude Code's proxy returns 401 (Unauthorized)
5. **Basic auth disabled by default**: Since [Java 8u111](https://confluence.atlassian.com/kb/basic-authentication-fails-for-outgoing-proxy-in-java-8u111-909643110.html), Basic authentication for HTTPS tunneling is disabled via `jdk.http.auth.tunneling.disabledSchemes=Basic`

### The Solution

The auth proxy acts as an authentication intermediary. See <proxy-alternatives.md> for detailed analysis of why alternatives (JVM settings, credential helpers, etc.) don't work.

- Accepts unauthenticated CONNECT requests from Bazel on `localhost:18081`
- Forwards them to the upstream proxy with proper `Proxy-Authorization: Basic` headers
- Handles credential refresh when JWTs are rotated (reads from file on each connection)
- Allows Bazel to access BCR without any Java authentication workarounds

## References

See <proxy-alternatives.md> for analysis of why alternatives don't work.

- [Claude Code on the Web](https://www.anthropic.com/news/claude-code-on-the-web) - Product announcement
- [Claude Code Sandboxing](https://www.anthropic.com/engineering/claude-code-sandboxing) - Network isolation architecture
- [Enterprise Network Configuration](https://docs.anthropic.com/en/docs/claude-code/corporate-proxy) - Proxy and CA configuration
- [Network Security](https://docs.anthropic.com/en/docs/claude-code/security#network-access) - Egress controls

## Configuration

All settings use [pydantic-settings](https://docs.pydantic.dev/latest/concepts/pydantic_settings/) with the `DUCKTAPE_CLAUDE_HOOKS_` prefix:

| Environment Variable                    | Default                             | Description                 |
| --------------------------------------- | ----------------------------------- | --------------------------- |
| `DUCKTAPE_CLAUDE_HOOKS_SUPERVISOR_DIR`  | `~/.config/claude-hooks/supervisor` | Supervisor config directory |
| `DUCKTAPE_CLAUDE_HOOKS_SUPERVISOR_PORT` | `19001`                             | Supervisor TCP port         |
| `DUCKTAPE_CLAUDE_HOOKS_AUTH_PROXY_DIR`  | `~/.cache/claude-hooks/auth-proxy`  | Proxy cache directory       |
| `DUCKTAPE_CLAUDE_HOOKS_AUTH_PROXY_PORT` | `18081`                             | Auth proxy port             |
| `DUCKTAPE_CLAUDE_HOOKS_SKIP_BAZELISK`   | `false`                             | Skip bazelisk download      |
| `DUCKTAPE_CLAUDE_HOOKS_SKIP_NIX`        | `false`                             | Skip nix installation       |
| `DUCKTAPE_CLAUDE_HOOKS_SKIP_PODMAN`     | `false`                             | Skip podman setup           |

See `settings.py` for the full configuration schema.

## Dependencies

See BUILD.bazel for the full dependency list. Key runtime requirements:

- **keytool** (from JDK) for Java truststore creation
- **Python 3.13+** for `ssl.SSLSocket.get_unverified_chain()` API

## Usage

### Via Supervisor (recommended)

The proxy runs under supervisor for automatic restarts and easy log access:

```bash
# View proxy status (use the Python that has supervisor installed)
python -m supervisor.supervisorctl -c ~/.config/claude-hooks/supervisor/supervisord.conf status auth-proxy

# Restart proxy (e.g., after credential refresh)
python -m supervisor.supervisorctl -c ~/.config/claude-hooks/supervisor/supervisord.conf restart auth-proxy

# View proxy logs (stdout)
tail -f ~/.config/claude-hooks/supervisor/auth-proxy.log

# View proxy errors (stderr)
tail -f ~/.config/claude-hooks/supervisor/auth-proxy.err.log

# Stop proxy
python -m supervisor.supervisorctl -c ~/.config/claude-hooks/supervisor/supervisord.conf stop auth-proxy
```

**Note**: Use the same Python interpreter that has the `supervisor` package installed. In Claude Code web environments, this is typically the interpreter from the claude-hooks uv tool environment.

### From session-start hook

The session-start hook (`session_start.py`) calls `proxy_setup.py` which performs
the proxy setup steps described above.

### Manual startup (for debugging)

For debugging only. Normal operation uses supervisor:

```bash
# The auth proxy reads upstream URL from a file (enables credential hot-reload)
# File format: http://username:password@host:port
claude-auth-proxy --listen-port 18081 --creds-file /path/to/upstream_proxy
```

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
           ├── 2. Writes to creds file (~/.cache/.../upstream_proxy)
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

1. **Session hook** starts the auth proxy daemon via supervisor
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

The auth proxy runs under supervisor:

- **Process Manager**: supervisord (`~/.config/claude-hooks/supervisor/supervisord.conf`)
- **Service Config**: `~/.config/claude-hooks/supervisor/conf.d/auth-proxy.conf`
- **Logging**: Stdout/stderr to `~/.config/claude-hooks/supervisor/auth-proxy.{log,err.log}`
- **Auto-restart**: Supervisor automatically restarts on crashes
- **Credentials**: Read from file on each connection (hot-reload)

## Verification

After session start:

```bash
# Verify supervisor is running the proxy
python -m supervisor.supervisorctl -c ~/.config/claude-hooks/supervisor/supervisord.conf status

# Proxy should be accessible
curl -s --max-time 5 -x http://127.0.0.1:18081 https://bcr.bazel.build/ | head -1

# Bazel should be able to access BCR
bazel info

# Check proxy logs
tail -20 ~/.config/claude-hooks/supervisor/auth-proxy.log
tail -20 ~/.config/claude-hooks/supervisor/auth-proxy.err.log
```

## Files

Supervisor files (in `~/.config/claude-hooks/supervisor/`):

- `supervisord.conf` - Supervisor main configuration
- `supervisord.{log,pid}` - Supervisor daemon state
- `conf.d/auth-proxy.conf` - Proxy service configuration
- `auth-proxy.{log,err.log}` - Proxy stdout/stderr logs

Note: Supervisor listens on TCP `127.0.0.1:19001` (no Unix socket file).

Setup files (in `~/.cache/claude-hooks/auth-proxy/`, created by `proxy_setup.py`):

- `upstream_proxy` - Upstream proxy credentials (read on each connection)
- `anthropic_ca.pem` - Extracted TLS inspection CA
- `combined_ca.pem` - System CAs + Anthropic CA bundle
- `cacerts.jks` - Java truststore with CA
- `bazelrc` - Auth proxy configuration (loaded via BAZEL_SYSTEM_BAZELRC_PATH)

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

## Development

```bash
# Run tests
bazel test //claude_hooks:test_proxy
```
