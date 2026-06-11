# Claude Code Sandbox Internals

How the Bash sandbox works on Linux, based on the
[sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime)
source and the leaked Claude Code v2.1.88 source tree.

## Mechanism

Every sandboxed Bash command is wrapped in a
[bubblewrap](https://github.com/containers/bubblewrap) (`bwrap`) invocation.
`bwrap` is a lightweight unprivileged container tool that uses Linux namespaces.

A command like `ls /tmp` becomes roughly:

```bash
bwrap --new-session --die-with-parent \
  --unshare-net \
  --unshare-pid --proc /proc \
  --dev /dev \
  --ro-bind / / \
  --bind /home/user/code/ducktape /home/user/code/ducktape \
  --bind /tmp/claude /tmp/claude \
  --bind ~/.cache/bazel ~/.cache/bazel \
  --ro-bind /dev/null ~/.bashrc \
  --setenv HTTP_PROXY http://localhost:3128 \
  --setenv HTTPS_PROXY http://localhost:3128 \
  --setenv CLAUDE_CODE_HOST_HTTP_PROXY_PORT 44095 \
  --setenv CLAUDE_CODE_HOST_SOCKS_PROXY_PORT 33943 \
  -- /usr/bin/zsh -c 'ls /tmp'
```

## Three Isolation Layers

### 1. Filesystem (allow-only writes)

The root filesystem is mounted read-only (`--ro-bind / /`). Specific paths
are then overlaid with read-write binds (`--bind`). The write allowlist is
built from:

- `.` (current working directory)
- Claude temp dir (`/tmp/claude`)
- Default system paths (`/dev/stdout`, `/dev/stderr`, `/dev/null`, etc.)
- `sandbox.filesystem.allowWrite` from settings (e.g., `~/.cache/bazel`)
- `additionalDirectories` from settings (e.g., `/code`)
- Paths from `Edit(...)` permission rules

Dangerous files get `/dev/null` mounted over them (mandatory deny):

- Shell configs: `.bashrc`, `.bash_profile`, `.zshrc`, `.zprofile`, `.profile`
- Git files: `.gitconfig`, `.gitmodules`, `.git/hooks`, `.git/config`
- IDE dirs: `.vscode/`, `.idea/`
- Claude dirs: `.claude/commands/`, `.claude/agents/`, `.claude/skills/`
- Settings: `.claude/settings.json`, `.claude/settings.local.json`

Source: `sandbox-adapter.ts` (`convertSettingsToSandboxConfig`),
`sandbox-utils.ts` (`DANGEROUS_FILES`, `getDangerousDirectories`).

### 2. Network (namespace isolation + proxy filtering)

`--unshare-net` creates a completely isolated network namespace — no
interfaces exist, not even loopback (initially). Inside the namespace:

1. `socat` bridges are started, forwarding TCP ports to Unix sockets that
   reach the host
2. The Unix sockets connect to an HTTP proxy and a SOCKS5 proxy running in
   the Claude Code host process
3. The proxies apply domain-based filtering (allowlist/denylist)
4. `HTTP_PROXY`/`HTTPS_PROXY` env vars point commands at the internal socat
   listeners

This means network access is:

- **Default deny**: no matching rule = blocked (or prompt user)
- **Domain-filtered**: only allowed domains pass through
- **Not deep-inspected**: domain fronting can bypass filters

Source: `sandbox-manager.ts` (`filterNetworkRequest`),
`linux-sandbox-utils.ts` (`initializeLinuxNetworkBridge`).

### 3. Unix sockets (seccomp BPF)

A seccomp BPF filter blocks `socket(AF_UNIX, ...)` syscalls, preventing
sandboxed commands from creating new Unix sockets (which could bypass the
network proxy by talking to host services directly).

The filter is applied in two stages:

1. **Outer bwrap** (no seccomp): creates namespaces, starts socat processes
   (socat needs Unix sockets to bridge)
2. **`apply-seccomp`** (seccomp active): applies BPF filter, then `exec`s
   the user command

Pre-built static binaries for x64 and arm64 live in
`vendor/seccomp/`. The filter does NOT block operations on inherited FDs —
only new socket creation.

Source: `linux-sandbox-utils.ts` (`wrapCommandWithSandboxLinux`),
`generate-seccomp-filter.ts`.

## Environment Differences (Sandboxed vs Unsandboxed)

| Env var                             | Sandboxed                                    | Unsandboxed         |
| ----------------------------------- | -------------------------------------------- | ------------------- |
| `HTTP_PROXY`                        | `http://localhost:3128` (socat inside bwrap) | inherited from host |
| `HTTPS_PROXY`                       | `http://localhost:3128`                      | inherited           |
| `ALL_PROXY`                         | `socks5://localhost:1080`                    | not set             |
| `NO_PROXY`                          | `localhost,127.0.0.1`                        | inherited           |
| `CLAUDE_CODE_HOST_HTTP_PROXY_PORT`  | host proxy port (e.g., `44095`)              | **not set**         |
| `CLAUDE_CODE_HOST_SOCKS_PROXY_PORT` | host SOCKS port (e.g., `33943`)              | **not set**         |
| `/proc`                             | fresh (isolated PID namespace)               | host `/proc`        |

There is **no explicit `SANDBOX=1` marker env var**. Detection methods:

- `CLAUDE_CODE_HOST_HTTP_PROXY_PORT` being set (only inside bwrap)
- `/proc` showing isolated PIDs (PID 1 is the shell, not systemd)
- Network interfaces being absent (`ip link` shows only loopback)
- Writes to non-allowlisted paths failing with EACCES/EROFS

## Write Allowlist: `additionalDirectories` vs `sandbox.filesystem.allowWrite`

Both end up in bwrap's `--bind` (read-write) list, but they differ in scope:

| Setting                         | Sandbox write | File tool access                         |
| ------------------------------- | ------------- | ---------------------------------------- |
| `additionalDirectories`         | Yes           | Yes (treated as working directory)       |
| `sandbox.filesystem.allowWrite` | Yes           | No (file tools need separate permission) |

Use `sandbox.filesystem.allowWrite` for paths that only Bash needs to write
(caches, build artifacts). Use `additionalDirectories` for paths that are
actual working directories (code repos).

## macOS

On macOS, the sandbox uses `sandbox-exec` with dynamically generated
[Seatbelt](https://reverse.put.as/wp-content/uploads/2011/09/Apple-Sandbox-Guide-v1.0.pdf)
profiles instead of bwrap. Seatbelt profiles support glob patterns natively
(unlike bwrap, which needs concrete paths). Violation monitoring reads from
the macOS system log store.

## References

- <https://github.com/anthropic-experimental/sandbox-runtime> — open-source
  sandbox runtime
- `/code/github.com/anthropics/claude-code-leaked/src/utils/sandbox/` — Claude
  Code adapter layer (from v2.1.88 source map leak)
- `/code/github.com/anthropic-experimental/sandbox-runtime/src/sandbox/` —
  runtime implementation
