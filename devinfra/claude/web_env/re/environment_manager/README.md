# environment-manager Reverse Engineering

Analysis and reconstruction of Anthropic's `environment-manager` binary — the
Go-based orchestration service that manages Claude Code web container sessions.

> **The binary is garble-obfuscated, including `-literals`.** Symbol names are
> randomized, `go version -m` returns "unknown", there is no DWARF, and string
> constants are encrypted so the binary's own help text does not appear in
> `strings` output. All of that is defeated — see [Working with the binary](#working-with-the-binary).
> Full function-level disassembly is available; RE does **not** need to fall
> back to string analysis.

## Target Binary

This README records evidence for binary `0b86a2a0` (`release-1186d93b9-ext`). The checked-in `src/`
corpus is a reconstruction based on the `a6f96673` DWARF extraction carried forward to `495ea204`,
with selected `RE(0b86a2a0)` annotations; it is not a verified source mirror of the pinned binary.

A newer live build, `release-ba76006550-ext`, was captured to `reference/environment-manager.gz` on
2026-09-12 and is **not yet reconciled** — see `../TODO.md`. That build ships with **no ELF Build ID
note at all** (`readelf -n` finds nothing), where every prior build had one; the table below stays
pinned to `0b86a2a0` until the reconstruction pass runs against the new build.

| Property           | Value                                                                                                                      |
| ------------------ | -------------------------------------------------------------------------------------------------------------------------- |
| **ELF Build ID**   | `0b86a2a0dbc9411eb18435e1c56822b0156f90fe`                                                                                 |
| **Reference file** | `devinfra/claude/web_env/reference/environment-manager.gz` (superseded by the `release-ba76006550-ext` capture, see above) |
| **Language**       | Go                                                                                                                         |
| **Version string** | `release-1186d93b9-ext`                                                                                                    |
| **Channel**        | Release                                                                                                                    |
| **Obfuscation**    | garble, with `-literals` (symbols randomized, strings encrypted)                                                           |
| **Binary size**    | 58.6 MB (uncompressed), 22 MB (gzipped)                                                                                    |
| **Functions**      | 46,573 (recovered from `.gopclntab`)                                                                                       |
| **Binary path**    | `/opt/env-runner/environment-manager`                                                                                      |
| **Dynamic deps**   | Only `libc.so.6`                                                                                                           |
| **RE directory**   | `src/` (flat layout, Build ID in this table)                                                                               |

## Working with the binary

Garble strips the ELF symbol table, so `go tool objdump` and `go tool nm` both
refuse the binary outright. Rebuild the symbol table first — then the entire
standard toolchain works, including call-target resolution:

```bash
bazel run //devinfra/skills/reverse_engineer/examples:gosymtab -- \
    /opt/env-runner/environment-manager /tmp/em.sym
go tool nm /tmp/em.sym | grep TaVHwGAw
go tool objdump -s '^main\.main$' /tmp/em.sym
```

String constants are encrypted, so anchoring on `strings` output does not work.
Recover them by letting the binary decrypt them and reading its memory:

```bash
gdb -batch -nx -ex 'set pagination off' -ex 'set confirm off' \
    -ex 'catch syscall exit_group' -ex 'run' -ex 'generate-core-file core' \
    --args /opt/env-runner/environment-manager --help
strings -n 4 core | sort -u > decrypted.txt   # core is ~1.8 GB; delete it after
```

Package and identifier names are randomized per build. <degarble_map.md> maps
the garbled tokens for this Build ID back to real packages, and explains how to
regenerate the map for a future binary.

Both techniques, and the pitfalls in each, are documented in the
`reverse_engineer` skill under "Defeating Obfuscation".

## Binary Name vs CLI Name

The binary is named `environment-manager` on disk but the Cobra root command
is `environment-runner`. All CLI examples in strings use `environment-runner`.

## Source Tree

The original Go module lives at `github.com/anthropics/anthropic/api-go/environment-manager`.

Garble randomizes application filenames per function, so DWARF names from older builds cannot
recover current source-file boundaries
([method](degarble_map.md#what-does-not-work-recovering-file-boundaries)). Use the [Build-ID
`0b86a2a0` package map](degarble_map.md#application-packages) for binary package attribution; it
does not cover the unreconciled newer capture. `src/` is the reconstruction described above, not a
recovered source tree or proof of runtime parity. The `495ea204` removals/additions remain
summarized in [Reconstruction Status](#reconstruction-status) and `BINDIFF_RESULTS.md`.

## CLI Commands

The binary exposes five subcommands via Cobra:

### `environment-runner setup`

Installs Claude Code, Sandbox Runtime, and language runtimes. Does NOT start
a session — use `orchestrator` or `task-run` for that.

```
environment-runner setup [flags]

Flags:
  --api-url <url>                       API base URL for connectivity healthcheck (default: api.anthropic.com)
  --claude-code-version <version>       Version to install (latest|stable|X.Y.Z) (default: latest)
  --log-level <level>                   Log level (debug|info|warn|error) (default: info)
  --sandbox-runtime-version <version>   Version to install (latest|X.Y.Z) (default: latest)
  --service-key-file <path>             Service key for API healthcheck (/v1/environments/whoami)
  --skip-claude-code                    Skip Claude Code installation
  --skip-sandbox-runtime                Skip sandbox-runtime installation
```

### `environment-runner orchestrator`

Runs the full session lifecycle: setup, lease management, session orchestration,
graceful shutdown. This is the primary entry point in production containers.

```
environment-runner orchestrator [flags]

Flags:
  --api-url <url>                       API base URL (default: api.anthropic.com)
  --client-id <id>                      Client/worker identifier (default: hostname)
  --environment-id <id>                 Environment ID (validated against whoami)
  --execute-hook <command>              Custom session handler command
  --execute-hook-timeout <duration>     Timeout for execute hook (0 = no timeout)
  --log-level <level>                   Log level (default: info)
  --loop-timeout <duration>             Loop timeout before triggering timeout hook (default: 5m)
  --max-poll-failures <n>               Max consecutive failures before exit (0 = infinite)
  --organization-id <id>                Organization ID (validated against whoami)
  --poll-hook <command>                 Custom polling command
  --poll-hook-timeout <duration>        Poll hook timeout (default: 30s)
  --poll-timeout <duration>             Poll request timeout (default: 5m)
  --reclaim-older-than-ms <ms>          Reclaim unacknowledged work items (0 = API default 5000ms)
  --sandbox-backend <backend>           sandbox-runtime (default) | none
  --sandbox-settings <path>             Path to sandbox-runtime settings JSON
  --service-key-file <path>             Path to environment service key file
  --skip-container-lock                 Skip container-level lock (dev/testing only)
  --skip-git-config                     Skip git configuration setup
  --timeout-hook <command>              Command to run on loop timeout
  --timeout-hook-timeout <duration>     Timeout hook timeout (default: 5m)
```

### `environment-runner task-run`

Executes a single task received via stdin. Used by `orchestrator` as the
default execute hook, or can be invoked directly.

```
environment-runner task-run [flags]

Flags:
  --allowed-tools <tools>               Comma-separated list of allowed tools
  --claude-agent-version <version>      Target Claude Agent version (latest|current|stable|X.Y.Z)
  --claude-path <path>                  Path to Claude CLI executable or wrapper binary
  --debug                               Enable debug mode
  --environment-id <id>                 Environment ID (required for self-hosted)
  --input-format <format>               v0 | v1 (default: v0)
  --local-append-system-prompt <text>   Additional system prompt to append locally
  --local-testing                       Disable WebSocket connections and git config
  --log-level <level>                   Log level (default: info)
  --organization-id <id>                Organization ID (required for self-hosted)
  --print-code-logs                     Print Claude Code logs on completion/failure
  --session <id>                        Session ID (required)
  --session-mode <mode>                 new | resume | resume-cached | setup-only (default: new)
  --skip-git-config                     Skip git configuration setup
  --stdin                               Deprecated: stdin is always used
  --upgrade-claude-code                 Deprecated: use --claude-agent-version
  --verbose-claude-logs                 Enable verbose Claude Agent output
  --working-directory <path>            Default working directory (default: /root)
```

### `environment-runner poll`

Makes a single poll request for work.

### `environment-runner preload-claude`

Pre-boots a Claude Code process ("a spare") so that a later `task-run` can claim
it instead of paying cold-start cost.

```text
Pre-boot a Claude Code spare for a later task-run to claim

Usage:
  environment-runner preload-claude [flags]

Flags:
      --claude-path string   Path to claude binary (default: resolved via GetClaudePath)
  -h, --help                 help for preload-claude
```

The claim path is instrumented with two metrics recovered from the binary:
`claude_code.spare.spawn_to_claim_window_ms` ("Wall-clock from spare
spawn/adopt to Claim (the overlap window W)") and `claude_code.spare.claim_miss`
("Cold spawn instead of warm-spare claim, attributed by reason").

### `environment-runner print-sandbox-settings`

Prints default sandbox configuration as JSON to stdout. Output is byte-identical
to the previous binary; see `reference/sandbox-settings.json`.

### `environment-runner completion`

Generates shell completion scripts (bash, zsh, fish, PowerShell).

## Architecture

For the `0b86a2a0` package map, see [`degarble_map.md`](degarble_map.md#application-packages); for
the companion process service, see [process_api RE](../process_api/README.md).

## Package Analysis

The source links below navigate the reconstruction only; they do not establish exact parity with
Build ID `0b86a2a0`. For binary package attribution and per-build names, use
[`degarble_map.md`](degarble_map.md#application-packages).

- **Lifecycle** — [`manager.go`](src/internal/manager/manager.go) composes environment type,
  sources, Claude, MCP and tunnel registration;
  [`orchestrator.go`](src/internal/orchestrator/orchestrator.go) runs setup, lease/heartbeat,
  work/session loop and signal shutdown, including process cleanup and lease release.
- **Process** — [`process.go`](src/internal/process/process.go),
  [`script.go`](src/internal/process/script.go): `Execute`, `ExecuteScript` and
  stream forwarding use the `process_api` WebSocket on port 2024; protocol details: [process_api
  RE](../process_api/README.md).
- **Claude** — [`claude_code_executor.go`](src/internal/claude/claude_code_executor.go):
  install/upgrade, first-run init, version check and outcome capture; session ingress token passes
  by file descriptor, not argv or environment. Install with
  `npm install -g @anthropic-ai/claude-code@<version>`, upgrade with `claude update` or npm
  reinstall, and initialize with `claude init`.
- **Sources and Git proxy** — [`git.go`](src/internal/sources/git.go),
  [`handler.go`](src/internal/gitproxy/handler.go): clone/fetch/checkout; proxy JWT or direct GitHub token authentication via
  [`GitHubSourceAuthProvider`](src/internal/auth/github_source_provider.go); Git protocol path
  validation and auth-token redaction in errors.
- **Sandbox** — [`runtime.go`](src/internal/sandbox/runtime.go),
  [`config.go`](src/internal/sandbox/config.go): sandbox-runtime wrapper and
  allowed-domain/network-rule validation; installation uses
  `npm install -g @anthropic-ai/sandbox-runtime@<version>`.
- **Tunnel** — [`tunnel.proto`](src/internal/tunnel/tunnelpb/tunnel.proto),
  [`deploy/action.go`](src/internal/tunnel/actions/deploy/action.go): HTTP/WebSocket tunnel,
  snapshot/status actions and `anthropic.sessions.tunnel.v1alpha`; deploy uses `filestore_url` and
  `filesystem_id`. Recorded protocol names: `HTTPTunnelRequest`, `HTTPTunnelResponseChunk`,
  `HTTPTunnelResponseHeaders`, `WSTunnelOpen`, `WSTunnelClose`, `WSTunnelMessage`,
  `WSTunnelOpened`, `WSTunnelError`. The reconstruction uses shorter response/WebSocket variant
  names; its naming does not establish the pinned binary's names.
- **MCP and auth** — [`internal/mcp/README.md`](src/internal/mcp/README.md),
  [`context.go`](src/internal/auth/context.go): `AuthContext` holds API/OAuth tokens, Session ID,
  session-ingress token and JWT; MCP tools provide GPG/SSH code signing.
  Supabase/Vercel/Antspace removals and related credential removals are listed below.
- **Environment types** — [`anthropic.go`](src/internal/envtype/anthropic/anthropic.go),
  [`byoc.go`](src/internal/envtype/byoc/byoc.go): Anthropic-managed install scripts versus BYOC and
  its `containProvideAuthRoundTripper`.
- **Observability** — [`metrics.go`](src/internal/o11y/metrics.go),
  [`otel_metrics.go`](src/internal/o11y/otel_metrics.go): OTLP HTTP and DataDog StatsD. Recorded
  metric names: `env_manager.claude_install`, `env_manager.language_setup`, `env_manager.env_init`,
  `claude_code_start`, `claude_code_end`, `orchestrator.timeout.count`, `tunnel_connect_start`,
  `tunnel_connect_retry_wait`, `heartbeat_successful`.
- **Pod monitor** — [`lease_manager.go`](src/internal/podmonitor/lease_manager.go): periodic lease
  heartbeat, `/tmp` health status and environment stop API on shutdown.

## Embedded Content

- Language scripts (sizes are from the binary inventory): `install_python.sh` (2.4 KB),
  `install_node.sh` (3.3 KB), `install_go.sh` (6.8 KB); they symlink to runtimes pre-installed in
  the image (`/opt/nodeNN/`, `/usr/local/goX.Y.Z/`).
- Hook templates: `stop-hook-baku.sh` checks Vite errors and uncommitted changes;
  `stop-hook-git-check.sh` checks uncommitted/unpushed changes; `session-start.sh` is the
  dependency-install hook template.
- Cobra completion scripts are embedded for bash, zsh, fish and PowerShell.

## Dependencies

### Direct Dependencies

| Module                                         | Version      | Purpose                  |
| ---------------------------------------------- | ------------ | ------------------------ |
| `github.com/anthropics/anthropic/api-go`       | `(devel)`    | Internal Anthropic API   |
| `github.com/spf13/cobra`                       | `v1.9.1`     | CLI framework            |
| `github.com/gorilla/websocket`                 | `v1.5.4-pre` | WebSocket (process_api)  |
| `github.com/mark3labs/mcp-go`                  | `v0.37.0`    | MCP server framework     |
| `github.com/DataDog/datadog-go/v5`             | `v5.8.2`     | StatsD metrics           |
| `connectrpc.com/connect`                       | `v1.19.1`    | Connect RPC              |
| `google.golang.org/grpc`                       | `v1.79.0`    | gRPC (tunnel protocol)   |
| `google.golang.org/protobuf`                   | `v1.36.12`   | Protobuf                 |
| `go.opentelemetry.io/otel`                     | `v1.39.0`    | OpenTelemetry SDK        |
| `go.opentelemetry.io/otel/exporters/otlp/...`  | various      | OTLP HTTP exporters      |
| `go.opentelemetry.io/contrib/bridges/otelslog` | `v0.13.0`    | slog → OTel bridge       |
| `github.com/cenkalti/backoff/v4`               | `v4.3.0`     | Exponential backoff      |
| `github.com/cenkalti/backoff/v5`               | `v5.0.3`     | Exponential backoff (v5) |
| `github.com/google/renameio/v2`                | `v2.0.0`     | Atomic file writes       |
| `github.com/google/uuid`                       | `v1.6.0`     | UUID generation          |
| `github.com/mitchellh/mapstructure`            | `v1.5.0`     | Map → struct decoding    |
| `github.com/buger/jsonparser`                  | `v1.1.1`     | Fast JSON parsing        |
| `golang.org/x/sync`                            | `v0.19.0`    | sync.ErrGroup, etc.      |

### Internal Dependencies

| Module                                               | Purpose                 |
| ---------------------------------------------------- | ----------------------- |
| `api-go/core/dogmetrics`                             | DataDog metrics wrapper |
| `api-go/gen/proto/anthropic/sessions/tunnel/v1alpha` | Tunnel protobuf defs    |

## API Endpoints Used

Extracted from strings and function analysis:

| Endpoint                                     | Method | Used by                |
| -------------------------------------------- | ------ | ---------------------- |
| `/v1/environments/whoami`                    | GET    | `whoami.go` (identity) |
| `/v1/environments/{id}/work/poll`            | POST   | `poller.go`            |
| `/v1/environments/{id}/work/{wid}/ack`       | POST   | `work_client.go`       |
| `/v1/environments/{id}/work/{wid}/heartbeat` | POST   | `lease_manager.go`     |
| `/v1/environments/{id}/work/{wid}/stop`      | POST   | `podmonitor`           |
| `/v1/code/sessions/{id}/worker/{wid}`        | POST   | `ccr_backend.go`       |
| `/v1/code/sessions/{id}/sign-commit`         | POST   | `sign_operations.go`   |
| `/v2/sessions/{id}`                          | GET    | `get_session_context`  |
| `/v2/sessions/{id}/events`                   | POST   | `session_ingress`      |
| `/v2/sessions/{id}/logs`                     | POST   | `session_ingress`      |
| `/v2/ccr-sessions/{id}/supabase-provision`   | POST   | REMOVED in 495ea204    |

## Artifacts

The `495ea204` binary is garble-obfuscated — Go tooling cannot extract module
info, symbols, or DWARF. Runtime probing and string analysis are the only
available RE methods:

```bash
# Runtime behavior (still works despite obfuscation)
/usr/local/bin/environment-manager --version
/usr/local/bin/environment-manager --help
/usr/local/bin/environment-manager setup --help
/usr/local/bin/environment-manager task-run --help
/usr/local/bin/environment-manager orchestrator --help
/usr/local/bin/environment-manager print-sandbox-settings

# String analysis
gunzip -k devinfra/claude/web_env/reference/environment-manager.gz
strings devinfra/claude/web_env/reference/environment-manager | sort -u

# Go tooling returns "unknown" or empty for obfuscated binary:
# go version -m "$BIN"     → "unknown"
# go tool nm "$BIN"         → (empty)
# go tool objdump "$BIN"    → (no annotated source lines)
```

## Key Differences from process_api RE

| Aspect            | process_api               | environment-manager                                                                              |
| ----------------- | ------------------------- | ------------------------------------------------------------------------------------------------ |
| Language          | Rust                      | Go                                                                                               |
| Debug info        | Stripped                  | garble-obfuscated (no DWARF, no syms)                                                            |
| Function evidence | ~30 application functions | 46,573 total functions recovered for 0b86a2a0 (includes runtime/vendor; not directly comparable) |
| Source files      | 10 decompiled files       | 78 files in the historical DWARF tree; current garble file grouping is not recoverable           |
| Complexity        | ~4,600 LoC                | ~17,800 LoC (reconstructed from a6f96673)                                                        |
| RE approach       | Ghidra decompilation      | Rebuilt symbol table + Go disassembly and runtime memory evidence; not string-only               |
| Build system      | Bazel rust_binary         | Bazel go_binary                                                                                  |

## Reconstruction Status

Source under `src/` was derived from the `a6f96673` DWARF-extracted
reconstruction. A binary diff between versions revealed significant code
changes — the source contains dead code that must be removed:

**Removed in 495ea204 (confirmed via binary diff):**

- Supabase MCP server (`internal/mcp/servers/supabase/`) -- entirely excised
- Vercel deploy backend (`internal/tunnel/actions/deploy/vercel.go`)
- Antspace deploy backend (`internal/tunnel/actions/deploy/antspace.go`)
- Baku project features (initialization, templates, settings bootstrap)
- Related auth fields (Supabase credentials, Vercel token, Antspace URL/token)

**Added in 495ea204:**

- `filestore_url` and `filesystem_id` JSON fields (new deploy mechanism)
- `jwt` JSON field (auth-related)

**Unchanged:** V0/V1 session context structs, CLI flags, sandbox settings, API endpoints.

`BINDIFF_RESULTS.md` records the historical binary comparison. Current
unresolved work across the environment-manager and `process_api`
reconstructions is tracked in <../TODO.md>; build-specific caveats remain in
the source tree's `TODO(re):` markers.
