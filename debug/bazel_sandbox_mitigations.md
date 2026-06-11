# Bazel Under Claude Code Sandbox — Mitigations

Investigated 2026-03-05.

## Current Status

This is a historical Claude Code CLI investigation note. The current canonical
configuration proposal for shared Bazel caches across worktrees, Codex local
sandboxing, and Claude Code local sandboxing is
<../devinfra/docs/bazel_worktree_cache_sharing.md>.

The filesystem fix should now use `sandbox.filesystem.allowWrite` for cache
directories such as `~/.cache/bazel` and `~/.cache/bazelisk`.
`permissions.additionalDirectories` is for real working/source directories, not
for generic build caches. The BES/RBE gRPC caveat below still matters for Claude
Code's local Linux sandbox: if an invocation needs reliable BuildBuddy BES/RBE,
use an explicit unsandboxed escape hatch for that invocation.

## Root Cause

`/wyrmhdd/bazel/repository_cache/` is not in the sandbox write allowlist.
`~/.bazelrc` sets `--repository_cache=/wyrmhdd/bazel/repository_cache`, and the
sandbox blocks writes there. Bazel reports `Read-only file system` during module
resolution (e.g., `rules_python` uv extension downloading a dist manifest).

Claude misattributes the error to "being offline" or "DNS broken" instead of
recognizing the sandbox restriction.

Network hosts for module resolution (`bcr.bazel.build`, `github.com`,
`files.pythonhosted.org`, etc.) are already in the sandbox network allowlist.
However, gRPC connections (used by BES and RBE to `remote.buildbuddy.io`) fail
with DNS resolution errors even though `remote.buildbuddy.io` is allowlisted and
HTTPS `curl` to the same host works.

### Why gRPC DNS fails (sandbox-runtime source analysis)

Source: `anthropic-experimental/sandbox-runtime` (v0.0.39).

The sandbox uses bubblewrap's `--unshare-net` to create an **isolated network
namespace** with no network interfaces. The only way out is via `socat` bridges
to two Node.js proxy servers on the host:

- **HTTP proxy** on port 3128 — handles HTTP/HTTPS via `CONNECT` tunneling
- **SOCKS5 proxy** on port 1080 — handles other TCP via `socks5h://`

The sandbox sets env vars including `GRPC_PROXY=socks5h://localhost:1080` (the
`h` means "resolve hostnames through the proxy"). Tools that respect
`HTTPS_PROXY` (like `curl`) work because they tunnel through the HTTP proxy.
But Bazel's gRPC client (Java, c-ares/Netty) **ignores `GRPC_PROXY`** and tries
direct DNS resolution via the system resolver — which fails because the network
namespace has no access to DNS servers on port 53.

### Can Bazel's gRPC be configured to use the proxy?

**No.** Investigated via Bazel source (`bazelbuild/bazel`, shallow clone).

Bazel has `--remote_proxy` and `--bes_proxy` flags, but they **only accept Unix
domain sockets** (`unix:/path/to/socket`). The implementation
(`GoogleAuthUtils.java:197-212`) opens a raw gRPC/TCP connection over the Unix
socket with `overrideAuthority` — it expects the other end to forward raw TCP to
the real gRPC server. It explicitly rejects non-`unix:` values.

The sandbox's Unix sockets (`/tmp/claude-http-XXX.sock`,
`/tmp/claude-socks-XXX.sock`) lead to HTTP and SOCKS5 **protocol** proxies, not
raw TCP forwarders. Protocol mismatch — Bazel would send gRPC frames, but the
proxy expects HTTP CONNECT or SOCKS5 handshakes.

gRPC-Java's `NettyChannelBuilder` also does **not** read `GRPC_PROXY`,
`HTTP_PROXY`, or `ALL_PROXY` env vars. Proxy support must be explicitly
configured by the application, and Bazel only does so for Unix sockets.

Bazel's `ProxyHelper.java` reads `HTTP_PROXY`/`HTTPS_PROXY` for **repository
downloads** (standard Java HTTP), but this is completely separate from the gRPC
remote execution/BES path.

**Conclusion:** Unfixable via configuration. Only `excludedCommands` works for
BES/RBE.

`~/.cache/bazel` (output base) is already in `additionalDirectories` and works
fine. HOME and TMPDIR are not changed by the sandbox in a way that affects Bazel.

## Options

### 1. `excludedCommands: ["bazel"]` (Nix config)

Add `"bazel"` to `sandbox.excludedCommands`. Commands matching this bypass the
sandbox entirely (filesystem + network).

```nix
# nix/home/claude_code/default.nix
excludedCommands = [ "nvidia-smi" "bazel" ];
```

- **Pros:** Simplest, most robust, no edge cases.
- **Cons:** Loses sandbox for bazel. But bazel is inherently high-trust (runs
  arbitrary build actions, downloads from the internet, compilers).

### 2. Add `/wyrmhdd/bazel` to `additionalDirectories` (Nix config)

```nix
additionalDirectories = [ "/code" "~/.cache/pre-commit" "/wyrmhdd/bazel" ]
  ++ cfg.additionalDirectories;
```

- **Pros:** Keeps sandbox active, targeted fix.
- **Cons:** Machine-specific (`/wyrmhdd` only on wyrm). May not cover unknown
  future paths. Can be per-host via the existing `cfg.additionalDirectories`
  option.

### 3. PreToolUse hook — deny sandboxed bazel with informative message

The hook's `tool_input` dict includes `dangerouslyDisableSandbox` when set.
Detect bazel commands running in sandbox and deny with a clear message:

```python
if hook_input.tool_name == "Bash":
    command = hook_input.tool_input.get("command", "")
    is_bazel = command.startswith("bazel ") or command == "bazel"
    sandbox_disabled = hook_input.tool_input.get("dangerouslyDisableSandbox", False)
    if is_bazel and not sandbox_disabled:
        return deny("Bazel must run outside the sandbox (needs write access to "
                     "repository cache). Retry with dangerouslyDisableSandbox: true.")
```

- **Pros:** Gives Claude an actionable error instead of cryptic `Read-only file
system`. Prevents "I must be offline" confusion.
- **Cons:** CLI-only (not web). Requires hook rebuild + release. Wastes a turn
  (deny → retry with sandbox disabled).

### 4. CLAUDE.md instruction

Tell Claude to always use `dangerouslyDisableSandbox: true` for bazel.

- **Pros:** No code changes.
- **Cons:** Not systematic, LLM may ignore it. Last resort.

## Recommendation

Option 1 + Option 3 together. Bazel provides minimal sandbox security value.
The hook is defense-in-depth for edge cases (wrapper scripts, piped commands).

## Experiment Log

### 2026-03-05: Option 2 tested

Added `/wyrmhdd/bazel` to `additionalDirectories` in `nix/home/hosts/wyrm.nix`:

```nix
programs.claude-code.additionalDirectories = [ "~/.cache/bazel" "/wyrmhdd/bazel" ];
```

**Results after `home-manager switch` + restart:**

- `bazel info` in sandbox: **works**
- `bazel build //util:env` in sandbox: **build succeeds** (15 action cache hits,
  1 internal)
- Repository cache writes to `/wyrmhdd/bazel/repository_cache/`: **fixed**
- Remote cache hits: **working** (saw "1 remote cache hit" in output)
- BES upload: **fails** — `UNAVAILABLE: Unable to resolve host remote.buildbuddy.io`
- RBE: **likely broken** — same gRPC DNS resolution issue

The filesystem fix worked. The remaining issue is gRPC DNS resolution under the
sandbox. `curl -s https://remote.buildbuddy.io` returns HTTP 415 (host
reachable), so the sandbox allows the connection — but Bazel's gRPC client
(c-ares based) resolves DNS differently than libc and fails.

### 2026-03-05: sandbox-runtime source analysis

Cloned `anthropic-experimental/sandbox-runtime` (v0.0.39) and analyzed the
network sandbox implementation. Confirmed the gRPC DNS failure is architectural:
the sandbox uses `--unshare-net` (full network namespace isolation) with proxy
bridges. Bazel's Java gRPC client doesn't respect `GRPC_PROXY` env var and
tries direct DNS resolution, which is impossible in the isolated namespace.

**Conclusion:** Option 2 (`additionalDirectories`) fixes filesystem issues but
cannot fix BES/RBE. Option 1 (`excludedCommands: ["bazel"]`) is the only way
to get full Bazel functionality including BES and RBE.
