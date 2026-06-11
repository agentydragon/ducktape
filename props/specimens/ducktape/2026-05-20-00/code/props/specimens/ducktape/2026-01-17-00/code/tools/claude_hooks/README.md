# Claude Web Hooks

Session hooks and Bazel proxy for Claude Code web environments.

## Components

- **Session Start Hook**: Sets up the development environment for Claude Code web sessions
- **Bazel Proxy**: Local proxy that adds authentication for TLS-inspecting proxies

## Session Start Hook

The hook runs at the start of each Claude Code web session and:

### Proxy Setup (via `proxy_setup.py`)

1. Starts supervisord for process management
2. Extracts the TLS inspection CA from the proxy via Python 3.13+ ssl APIs
3. Creates a Java truststore with the CA using pyjks
4. Registers the proxy with supervisor at `127.0.0.1:18081`
5. Creates combined CA bundle (system CAs + proxy CA)
6. Writes bazelrc to `~/.cache/bazel-proxy/bazelrc`

### Bazel Setup (via `bazelisk_setup.py`)

7. Downloads and installs Bazelisk
8. Creates wrapper script at `~/.cache/bazel-proxy/bin/bazel`

### Development Tools (via `binary_tools.py`)

9. Installs cluster tools (opentofu, tflint, flux, kustomize, kubeseal, helm)
10. Installs dev tools (alejandra for Nix formatting)
11. Installs nix (for nix eval, flake operations)

### Environment Configuration

12. Configures podman for gVisor compatibility
13. Sets up environment variables in `CLAUDE_ENV_FILE`
14. Installs git pre-commit hooks

See `.claude/settings.json` for hook configuration.

# Bazel Proxy

A local proxy that adds authentication headers for upstream TLS-inspecting proxies, enabling Bazel to access the Bazel Central Registry (BCR).

## Why This Exists

[Claude Code on the web](https://docs.anthropic.com/en/docs/claude-code/claude-code-on-the-web) runs in ephemeral containers with a TLS-inspecting proxy for network egress. This breaks Bazel's access to BCR due to multiple Java/JVM limitations:

### The Problem

1. **TLS Inspection**: The proxy does TLS inspection with a custom Anthropic CA certificate
2. **JWT Authentication**: Proxy credentials include a JWT token for authentication (see [network docs](https://docs.anthropic.com/en/docs/claude-code/security#network-access))
3. **Java doesn't read env vars**: Standard Java networking uses system properties (`https.proxyHost`), not `HTTPS_PROXY` environment variables
4. **HTTP 401 vs 407**: Java's `Authenticator` class only triggers on HTTP 407 (Proxy Authentication Required), but Claude Code's proxy returns 401 (Unauthorized)
5. **Basic auth disabled by default**: Since [Java 8u111](https://confluence.atlassian.com/kb/basic-authentication-fails-for-outgoing-proxy-in-java-8u111-909643110.html), Basic authentication for HTTPS tunneling is disabled via `jdk.http.auth.tunneling.disabledSchemes=Basic`

### The Solution

This local proxy acts as an authentication intermediary. See <proxy-alternatives.md> for detailed analysis of why alternatives (JVM settings, credential helpers, etc.) don't work.

- Accepts unauthenticated CONNECT requests from Bazel on `localhost:18081`
- Forwards them to the upstream proxy with proper `Proxy-Authorization: Basic` headers
- Handles credential refresh when JWTs are rotated (reads from file on each connection)
- Allows Bazel to access BCR without any Java authentication workarounds

## References

See <proxy-alternatives.md> for details.

- [Claude Code on the Web](https://docs.anthropic.com/en/docs/claude-code/claude-code-on-the-web) - Container environment overview
- [Network Configuration](https://docs.anthropic.com/en/docs/claude-code/security#network-access) - Proxy and network egress details
- [Enterprise Configuration](https://docs.anthropic.com/en/docs/claude-code/enterprise) - TLS certificate configuration

## Dependencies

This package has the following dependencies (see BUILD.bazel):

- cryptography (TLS certificate parsing)
- mako (template rendering)
- pproxy (proxy server)
- pyjks (Java keystore manipulation)
- pyjwt (JWT decoding)
- supervisor (process management)

**Note**: Requires Python 3.13+ for `ssl.SSLSocket.get_unverified_chain()` API.

## Usage

### Via Supervisor (recommended)

The proxy runs under supervisor for automatic restarts and easy log access:

```bash
# View proxy status
supervisorctl -c ~/.config/supervisor/supervisord.conf status bazel-proxy

# Restart proxy (e.g., after credential refresh)
supervisorctl -c ~/.config/supervisor/supervisord.conf restart bazel-proxy

# View proxy logs (stdout)
tail -f ~/.config/supervisor/bazel-proxy.log

# View proxy errors (stderr)
tail -f ~/.config/supervisor/bazel-proxy.err.log

# Stop proxy
supervisorctl -c ~/.config/supervisor/supervisord.conf stop bazel-proxy
```

### From session-start hook

The session-start hook (`session_start.py`) calls `proxy_setup.py` which:

1. Starts supervisord for process management
2. Extracts the TLS inspection CA from the proxy via Python 3.13+ ssl APIs
3. Creates a Java truststore with the CA using pyjks
4. Registers the proxy with supervisor at `127.0.0.1:18081`
5. Creates combined CA bundle (system CAs + proxy CA)
6. Writes bazelrc to `~/.cache/bazel-proxy/bazelrc` (loaded via `BAZEL_SYSTEM_BAZELRC_PATH`)

### Manual startup (for debugging)

For debugging only. Normal operation uses supervisor:

```bash
# Read credentials from file (in normal operation, comes from https_proxy env var)
pproxy -l http://127.0.0.1:18081/ -r http://upstream:port#user:pass/
```

## How It Works

1. Session hook reads `https_proxy` from environment
2. Builds pproxy command with credentials embedded in upstream URI
3. Registers pproxy as a supervisor service
4. pproxy listens for CONNECT requests on local port
5. pproxy forwards to upstream with `Proxy-Authorization: Basic ...` header

Credential refresh: handled during proxy startup via `proxy_setup.ensure_proxy_running()`,
which detects credential changes and updates the supervisor service config.

## Lifecycle Management

The proxy (pproxy) runs under supervisor:

- **Process Manager**: supervisord (`~/.config/supervisor/supervisord.conf`)
- **Service Config**: `~/.config/supervisor/conf.d/bazel-proxy.conf`
- **Logging**: Stdout/stderr to `~/.config/supervisor/bazel-proxy.{log,err.log}`
- **Auto-restart**: Supervisor automatically restarts on crashes
- **Credentials**: Embedded in command (service config updated on refresh)

## Verification

After session start:

```bash
# Verify supervisor is running the proxy
supervisorctl -c ~/.config/supervisor/supervisord.conf status

# Proxy should be accessible
curl -s --max-time 5 -x http://127.0.0.1:18081 https://bcr.bazel.build/ | head -1

# Bazel should be able to access BCR
bazel info

# Check proxy logs
tail -20 ~/.config/supervisor/bazel-proxy.log
tail -20 ~/.config/supervisor/bazel-proxy.err.log
```

## Files

Supervisor files (in `~/.config/supervisor/`):

- `supervisord.conf` - Supervisor main configuration
- `supervisord.{log,pid}` - Supervisor daemon state
- `supervisor.sock` - Supervisor control socket
- `conf.d/bazel-proxy.conf` - Proxy service configuration
- `bazel-proxy.{log,err.log}` - Proxy stdout/stderr logs

Setup files (in `~/.cache/bazel-proxy/`, created by `proxy_setup.py`):

- `upstream_proxy` - Upstream proxy credentials (read on each connection)
- `anthropic_ca.pem` - Extracted TLS inspection CA
- `combined_ca.pem` - System CAs + Anthropic CA bundle
- `cacerts.jks` - Java truststore with CA
- `bazelrc` - Bazel proxy configuration (loaded via BAZEL_SYSTEM_BAZELRC_PATH)

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

## Development

```bash
# Run tests
bazel test //claude_hooks:test_proxy
```
