# Claude Code Sandbox Internals

## WebFetch domain rules and Bazel incompatibility

Claude Code's sandbox (bubblewrap + proxy) has an interaction between
`WebFetch(domain:...)` permissions and Bazel that prevents them from
coexisting.

### Mechanism

The sandbox adapter (`sandbox-adapter.ts`) converts settings into a
`SandboxRuntimeConfig`. It always defines `network.allowedDomains` as an
array — empty when no `WebFetch(domain:...)` rules exist, populated when
they do.

Because `allowedDomains` is always defined (even as `[]`), the sandbox
manager (`sandbox-manager.js`) always considers `hasNetworkConfig = true`,
which means:

1. `--unshare-net` is applied to bubblewrap, isolating the network namespace.
2. An HTTP CONNECT proxy + SOCKS proxy are started on the host.
3. Proxy sockets are bind-mounted into the sandbox so commands can reach them.

When a sandboxed command makes an outbound connection, the proxy calls
`filterNetworkRequest()`, which checks the host against `allowedDomains`:

- **Match found** → allow immediately.
- **No match** → call `sandboxAskCallback`, which shows the user a prompt:
  "Allow network connection to X?" with options: Yes / Yes, don't ask again / No.

Picking "don't ask again" writes `WebFetch(domain:X)` to
`.claude/settings.local.json` via `persistPermissionUpdate()`. This is where
the self-reinforcing cycle starts.

### The self-reinforcing cycle

1. Start with no `WebFetch(domain:...)` rules → `allowedDomains = []`.
2. `hasNetworkConfig` is still `true` (array is defined, just empty).
3. Proxy runs, `--unshare-net` is applied. On Linux, proxy sockets are
   bind-mounted into the sandbox so connections can still work through the
   proxy.
4. Every outbound connection to an unlisted domain triggers the prompt.
5. User picks "don't ask again" → `WebFetch(domain:X)` written to
   `settings.local.json`.
6. On next sandbox invocation, `allowedDomains` is now non-empty.
7. **On Linux**, the proxy is supposed to work, but `--unshare-net` blocks
   TCP loopback — including Bazel's gRPC client-server protocol (port in
   `<output_base>/server/command_port`). Bazel can't reach its server or
   RBE endpoints (e.g. `remote.buildbuddy.io`).

### Why the prompts appear even with zero domain rules

The proxy always runs because `allowedDomains` is always defined (as `[]`).
With no entries in the list, no host matches → every outbound connection
falls through to the "ask user" path. There is no "allow all" wildcard —
`matchesDomainPattern()` only supports `*.example.com` subdomain wildcards
and exact matches, no bare `*`.

### Current workaround

Global settings use a `WebFetch(domain:...)` allowlist (`allowedWebFetchDomains`
in `default.nix`). This auto-approves known domains via the proxy without
prompting, but triggers `--unshare-net` on every sandbox invocation.

**Tradeoff accepted:** Bazel commands must always use
`dangerouslyDisableSandbox: true`. This is documented in `AGENTS.md`.

Domains not on the allowlist will still prompt. "Yes" only approves for
the current request — the next sandboxed command will prompt again. The
only durable option is "don't ask again", which writes to
`settings.local.json` (avoid this in Bazel projects).

### Per-project trap

Adding `WebFetch(domain:...)` rules to any project's
`.claude/settings.local.json` — whether manually or by approving "don't
ask again" prompts — merges them into the effective permissions and
populates `allowedDomains`. This breaks Bazel builds in that project,
including auto-allowed commands like `Bash(bazelisk build *)`.

**Rule:** never approve "don't ask again" for domain prompts in projects
that need Bazel. Periodically strip `WebFetch(domain:...)` entries from
`.claude/settings.local.json` if they accumulate.

Note: "Yes" does **not** persist for the session either. The proxy has no
in-memory cache for ad-hoc approvals — `filterNetworkRequest()` re-checks
`config.network.allowedDomains` on every connection, and "Yes" only resolves
the current promise without adding the host. Concurrent connections to the
same host within one sandboxed command are batch-resolved, but the next
sandboxed command will prompt again for the same host. The only way to
suppress prompts durably is "don't ask again" — which writes to settings
and breaks Bazel.

### Escape hatch

For commands that need network access and are blocked by the sandbox,
use `dangerouslyDisableSandbox: true`. This completely bypasses the
sandbox for that invocation — no proxy, no `--unshare-net`, no prompts.

### Key source files

- `src/utils/sandbox/sandbox-adapter.ts` — converts settings to runtime config,
  extracts `allowedDomains` from `WebFetch(domain:...)` permission rules
- `src/components/permissions/SandboxPermissionRequest.tsx` — UI for the
  domain approval dialog
- `src/screens/REPL.tsx` (~line 4620) — wires "don't ask again" to
  `persistPermissionUpdate()` which writes `WebFetch(domain:...)` to
  `settings.local.json`
- `@anthropic-ai/sandbox-runtime/dist/sandbox/sandbox-manager.js` —
  `filterNetworkRequest()` checks domains and prompts; `wrapCommand()` decides
  `--unshare-net` based on `hasNetworkConfig`
- `@anthropic-ai/sandbox-runtime/dist/sandbox/linux-sandbox-utils.js` —
  applies `--unshare-net` and bind-mounts proxy sockets
- `@anthropic-ai/sandbox-runtime/dist/sandbox/http-proxy.js` — HTTP CONNECT
  proxy with `options.filter` callback
