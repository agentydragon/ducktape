# environment-manager Reverse Engineering

Analysis and reconstruction of Anthropic's `environment-manager` binary — the
Go-based orchestration service that manages Claude Code web container sessions.

> **Current binary is garble-obfuscated.** The previous (`a6f96673`, staging)
> had full DWARF + symbols. The current (`64bc4dc1`, release) uses
> [garble](https://github.com/burrowers/garble) — symbol names are randomized,
> `go version -m` returns "unknown", DWARF extraction is not possible. RE now
> relies on runtime behavior and string analysis only.

## Target Binary

| Property           | Value                                                      |
| ------------------ | ---------------------------------------------------------- |
| **ELF Build ID**   | `64bc4dc1a5a3a38ce5732655f7fdfbeb62b8598d`                 |
| **Reference file** | `devinfra/claude/web_env/reference/environment-manager.gz` |
| **Language**       | Go                                                         |
| **Version string** | `release-9f4ec76fbc-ext`                                   |
| **Channel**        | Release (was staging)                                      |
| **Obfuscation**    | garble (all symbol names randomized)                       |
| **Binary size**    | 49.8 MB (uncompressed), 19 MB (gzipped)                    |
| **Binary path**    | `/opt/env-runner/environment-manager`                      |
| **Dynamic deps**   | Only `libc.so.6`                                           |
| **RE directory**   | `64bc4dc1/`                                                |

## Binary Name vs CLI Name

The binary is named `environment-manager` on disk but the Cobra root command
is `environment-runner`. All CLI examples in strings use `environment-runner`.

## Source Tree

The original Go module lives at
`github.com/anthropics/anthropic/api-go/environment-manager`. Source tree
was extracted from `a6f96673` DWARF debug info (87 files). Binary diff
(2026-03-26) confirmed that 64bc4dc1 removed several packages (Supabase,
Vercel, Antspace, Baku) and the old binary's DWARF paths revealed files
missing from the RE source tree. Items marked `REMOVED` are confirmed
absent from the 64bc4dc1 binary.

```
main.go                                         # Entry point: Cobra root command + Version
cmd/
  cmd_code_sign.go                              # Code-sign subcommand
  cmd_orchestrator.go                           # Full session lifecycle orchestrator
  cmd_poll.go                                   # Work polling loop
  cmd_print_sandbox_settings.go                 # Print sandbox config as JSON
  cmd_setup.go                                  # Environment setup (install deps)
  cmd_task_run.go                               # Execute a single task
  utils.go                                      # Shared CLI helpers
internal/
  api/
    ccr_backend.go                              # CCR v2 backend (RegisterWorker, WorkerEpoch)
    client.go                                   # HTTP client with auth
    get_session_context.go                      # Fetch session context from API
    noop_backend.go                             # No-op backend for setup-only mode
    retry.go                                    # Retry logic with backoff
    session_ingress_backend.go                  # Session ingress backend
    session_ingress_client.go                   # Session event ingress client
    session_ingress_types.go                    # Ingress event types
    work_client.go                              # Work poll/acknowledge client
  auth/
    context.go                                  # Auth context (API token, OAuth, session ID)
    github_source_provider.go                   # GitHub App auth for source repos
  claude/
    claude_code_executor.go                     # Execute Claude Code CLI
    config_helpers.go                           # Claude config file manipulation
    init.go                                     # Claude Code init (first run)
    install.go                                  # Install/upgrade Claude Code via npm
    outcomes.go                                 # Parse Claude Code outcomes
    session_urls.go                             # Session URL builder
  config/
    config.go                                   # Session/source/environment config types
    session_mode.go                             # Session mode enum
  envtype/
    envtype.go                                  # Environment type interface
    anthropic/
      anthropic.go                              # Anthropic-managed environment type
      config.go                                 # Anthropic env config
      install_scripts/
        scripts.go                              # Embedded language install scripts
    byoc/
      byoc.go                                   # BYOC (Bring Your Own Cloud) environment
    shared/                                     # Shared embedded content (settings, hooks)
  gitproxy/
    handler.go                                  # Git HTTP handler (smart protocol)
    manager.go                                  # Git proxy lifecycle manager
    server.go                                   # Git proxy HTTP server
  input/
    parser.go                                   # Input parser interface
    secret.go                                   # Work secret decoding
    v0_parser.go                                # V0 input format parser
    v1_parser.go                                # V1 input format parser
  logger/
    file_logger.go                              # File-based slog handler
    log_writer.go                               # io.Writer to slog bridge
    multi_handler.go                            # Fan-out slog handler
  manager/
    manager.go                                  # Top-level session manager
    mcp.go                                      # MCP server management
    skill_extraction.go                         # Skill extraction from repos
    tunnel_register.go                          # Tunnel registration
  mcp/
    registry.go                                 # MCP server registry
    server.go                                   # Base MCP server
    servers/codesign/
      registration.go                           # Code-sign MCP registration
      server.go                                 # Code-sign MCP server
      sign_operations.go                        # Signing operations
      types.go                                  # Code-sign type definitions
    servers/supabase/                           # REMOVED in 64bc4dc1
      client.go                                 # REMOVED - Supabase REST client
      functions.go                              # REMOVED - Function deploy
      registration.go                           # REMOVED - MCP registration
      server.go                                 # REMOVED - MCP server
  o11y/
    discard_o11y_service.go                     # No-op observability
    metric_types.go                             # Metric type definitions
    metrics.go                                  # Metric recording helpers
    o11y_service.go                             # Observability service interface
    otel_logging.go                             # OTel log exporter init
    otel_metrics.go                             # OTel metric exporter init
    util.go                                     # O11y utilities
    diag/
      cc_log_collector.go                       # Claude Code log collector
      diag_logs.go                              # Diagnostic log service
  orchestrator/
    hooks.go                                    # Orchestrator lifecycle hooks
    orchestrator.go                             # Main orchestrator loop
    poll_hook.go                                # Polling hook implementation
    poller.go                                   # Work poller
    whoami.go                                   # /v1/environments/whoami client
  podmonitor/
    lease_manager.go                            # Container lease heartbeat manager
  process/
    process.go                                  # Process execution via process_api WS
    script.go                                   # Script execution helpers
  sandbox/
    config.go                                   # Sandbox config validation
    install.go                                  # Sandbox runtime installation
    runtime.go                                  # Sandbox runtime wrapper
  session/
    session_activity_recorder.go                # Session activity recording
    noop_activity_recorder.go                   # No-op activity recorder
  sources/
    git.go                                      # Git source handler (clone/fetch)
    sources.go                                  # Source handler manager
  tunnel/
    client.go                                   # Tunnel WebSocket/gRPC client
    handler.go                                  # HTTP tunnel handler
    ws_handler.go                               # WebSocket tunnel handler
    actions/
      registry.go                               # Tunnel action registry
      deploy/
        action.go                               # Deploy action (filestore-based)
        antspace.go                             # REMOVED in 64bc4dc1
        vercel.go                               # REMOVED in 64bc4dc1
      snapshot/
        action.go                               # Snapshot action
      status/
        action.go                               # Status action
  util/
    git.go                                      # Git helper utilities
    lockfile.go                                 # File-based advisory locking
    net.go                                      # Network utilities
    periodic_invoker.go                         # Periodic task runner
    retry.go                                    # Retry with backoff
    stream.go                                   # Stream utilities
    tailer.go                                   # File tailer
```

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

### `environment-runner print-sandbox-settings`

Prints default sandbox configuration as JSON to stdout.

### `environment-runner completion`

Generates shell completion scripts (bash, zsh, fish, PowerShell).

## Architecture

```
┌────────────────────────────────────────────────────────────────┐
│                    environment-manager                          │
│                                                                │
│  ┌─────────────────────────┐   ┌────────────────────────────┐  │
│  │     Cobra CLI           │   │     Orchestrator           │  │
│  │  setup | orchestrator   │──►│  lease mgmt, setup,        │  │
│  │  task-run | poll        │   │  session loop, shutdown    │  │
│  │  print-... | completion │   └────────────┬───────────────┘  │
│  └─────────────────────────┘                │                  │
│                                             ▼                  │
│  ┌──────────────┐  ┌───────────┐  ┌─────────────────────┐     │
│  │  Manager     │  │  Sources  │  │  Claude Code        │     │
│  │  session     │  │  git      │  │  install/upgrade    │     │
│  │  lifecycle   │  │  clone    │  │  execute via        │     │
│  │              │  │  fetch    │  │  process_api WS     │     │
│  └──────┬───────┘  └─────┬────┘  └──────────┬──────────┘     │
│         │                │                   │                 │
│  ┌──────▼───────┐  ┌─────▼────┐  ┌──────────▼──────────┐     │
│  │  Sandbox     │  │ Git      │  │  process_api        │     │
│  │  Runtime     │  │ Proxy    │  │  WebSocket client   │     │
│  │  wrapper     │  │ HTTP     │  │  (port 2024)        │     │
│  └──────────────┘  └──────────┘  └─────────────────────┘     │
│                                                                │
│  ┌──────────────┐  ┌───────────┐  ┌─────────────────────┐     │
│  │  Tunnel      │  │  MCP      │  │  Observability     │     │
│  │  WS + HTTP   │  │  servers  │  │  OTel + DataDog    │     │
│  │  gRPC proto  │  │  codesign │  │  diag logs         │     │
│  └──────────────┘  └───────────┘  └─────────────────────┘     │
│                                                                │
│  ┌──────────────┐  ┌───────────┐  ┌─────────────────────┐     │
│  │  Auth        │  │  Config   │  │  Pod Monitor       │     │
│  │  API token   │  │  session  │  │  lease heartbeat   │     │
│  │  OAuth       │  │  sources  │  │  health file       │     │
│  │  GitHub App  │  │  env type │  │                    │     │
│  └──────────────┘  └───────────┘  └─────────────────────┘     │
└────────────────────────────────────────────────────────────────┘
```

## Package Analysis

### Core Lifecycle (`cmd/`, `internal/manager/`, `internal/orchestrator/`)

The orchestrator is the central loop:

1. **Setup phase**: Install Claude Code + Sandbox Runtime, configure languages
2. **Lease acquisition**: Obtain container lease via API, start heartbeat
3. **Session loop**: Wait for work → process session → report results → repeat
4. **Shutdown**: Graceful on SIGTERM/SIGINT, kill processes, release lease

The `manager` package ties together environment type, sources, Claude execution,
MCP servers, and tunnel registration.

### Process Execution (`internal/process/`)

Communicates with `process_api` over WebSocket (port 2024) to spawn and manage
child processes. Uses the same protocol documented in
<../process_api/README.md>.

Key functions:

- `Execute()` — spawn a process via `CreateProcess`, stream I/O
- `ExecuteScript()` — run a shell script
- `streamPipe()` — bidirectional stdin/stdout/stderr forwarding

### Claude Code Management (`internal/claude/`)

- **Installation**: `npm install -g @anthropic-ai/claude-code@<version>`
- **Upgrade**: `claude update` CLI or reinstall via npm
- **Initialization**: `claude init` for first-run setup
- **Execution**: Invoke Claude Code with session context, capture outcomes
- **Token injection**: Pass API token via file descriptor (not env var)
- **Version checking**: Compare installed vs requested version

### Source Management (`internal/sources/`, `internal/gitproxy/`)

- **Git handler**: Clone repositories, fetch updates, checkout branches
- **Git proxy**: HTTP server implementing Git smart protocol, with JWT auth
  from GitHub App tokens. Validates git paths, sanitizes errors.
- **Source processing**: Iterate over session sources, clone/fetch each

### Sandbox (`internal/sandbox/`)

- **Config validation**: Validate sandbox settings (allowed domains, network rules)
- **Installation**: `npm install -g @anthropic-ai/sandbox-runtime@<version>`
- **Runtime wrapper**: Wrap Claude Code execution in sandbox
- **Domain matching**: Check if domains match allow patterns

### Tunnel System (`internal/tunnel/`)

Bidirectional HTTP/WebSocket tunneling using gRPC + protobuf
(`anthropic.sessions.tunnel.v1alpha`):

- **Client**: WebSocket + gRPC tunnel client
- **HTTP handler**: Forward HTTP requests from tunnel to local services
- **WS handler**: Forward WebSocket connections through tunnel
- **Deploy action**: Deployment through tunnel using filestore-based mechanism
  (`filestore_url`, `filesystem_id` fields). **Note:** Vercel and Antspace
  backends were removed in 64bc4dc1.
- **Snapshot action**: Project state snapshot (file listings, git status)
- **Status action**: Health check (port validation, "ok" response)

Protobuf messages: `HTTPTunnelRequest`, `HTTPTunnelResponseChunk`,
`HTTPTunnelResponseHeaders`, `WSTunnelOpen`, `WSTunnelClose`,
`WSTunnelMessage`, `WSTunnelOpened`, `WSTunnelError`.

### MCP Servers (`internal/mcp/`)

- **Registry**: Register/unregister MCP servers with Claude Code
- **Base server**: Common MCP server infrastructure (using `mcp-go` v0.37.0)
- **Code-sign server**: GPG/SSH code signing via MCP tool calls
- **Supabase server**: REMOVED in 64bc4dc1 (was: database provisioning,
  migrations, function deploy, type generation)

### Authentication (`internal/auth/`)

- `AuthContext`: Holds API token (Anthropic), OAuth token, session ID,
  session ingress token. **Note:** Vercel deploy token, Antspace control plane
  URL + auth token, and Supabase credentials were removed in 64bc4dc1.
- `GitHubSourceAuthProvider`: GitHub App authentication for source repos

### Environment Types (`internal/envtype/`)

Two environment types:

- **`anthropic`**: Anthropic-managed infrastructure. Copies install scripts
  from embedded filesystem, manages language installations.
- **`byoc`**: Bring Your Own Cloud. Customer-managed infrastructure with
  custom auth round-tripper (`containProvideAuthRoundTripper`).

### Observability (`internal/o11y/`)

Dual exporter: OpenTelemetry (OTLP HTTP) + DataDog StatsD.

Metrics include: `env_manager.claude_install`, `env_manager.language_setup`,
`env_manager.env_init`, `claude_code_start`, `claude_code_end`,
`orchestrator.timeout.count`, `tunnel_connect_start`,
`tunnel_connect_retry_wait`, `heartbeat_successful`.

### Pod Monitor (`internal/podmonitor/`)

Lease-based container lifecycle:

- **Heartbeat**: Periodic POST to session endpoint to extend lease
- **Health file**: Write health status to `/tmp` for external monitoring
- **Stop API**: Call environment stop endpoint on shutdown

## Embedded Content

### Language Install Scripts

Three bash scripts embedded via Go `embed.FS` in
`internal/envtype/anthropic/install_scripts/`:

| Script                       | Purpose                                    |
| ---------------------------- | ------------------------------------------ |
| `install_python.sh` (2.4 KB) | Set up Python version symlinks             |
| `install_node.sh` (3.3 KB)   | Set up Node.js version symlinks            |
| `install_go.sh` (6.8 KB)     | Set up Go version symlinks and environment |

These scripts configure language runtimes that are pre-installed in the
container image (under `/opt/nodeNN/`, `/usr/local/goX.Y.Z/`, etc.) by
creating appropriate symlinks.

### Claude Code Hook Templates

The binary contains embedded templates for session-start and stop hooks
(the "session-start hook skill"), including:

- `stop-hook-baku.sh` — checks for Vite dev server errors and uncommitted changes
- `stop-hook-git-check.sh` — checks for uncommitted/unpushed changes
- `session-start.sh` — dependency installation hook template

### Cobra Shell Completions

Full shell completion scripts for bash, zsh, fish, and PowerShell are embedded
via Cobra's built-in completion generation.

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
| `google.golang.org/protobuf`                   | `v1.36.11`   | Protobuf                 |
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
| `/v2/ccr-sessions/{id}/supabase-provision`   | POST   | REMOVED in 64bc4dc1    |

## Artifacts

The `64bc4dc1` binary is garble-obfuscated — Go tooling cannot extract module
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

| Aspect       | process_api           | environment-manager                       |
| ------------ | --------------------- | ----------------------------------------- |
| Language     | Rust                  | Go                                        |
| Debug info   | Stripped              | garble-obfuscated (no DWARF, no syms)     |
| Functions    | ~30 application       | Unknown (symbols randomized)              |
| Source files | 10 files (decompiled) | 78 files (from previous DWARF version)    |
| Complexity   | ~4,600 LoC            | ~17,800 LoC (reconstructed from a6f96673) |
| RE approach  | Ghidra decompilation  | Runtime behavior + string analysis        |
| Build system | Bazel rust_binary     | Bazel go_binary                           |

## Reconstruction Status

Source under `64bc4dc1/src/` was derived from the `a6f96673` DWARF-extracted
reconstruction. A binary diff (2026-03-26) between the two versions revealed
significant code changes -- the source contains dead code that must be removed:

**Removed in 64bc4dc1 (confirmed via binary diff):**

- Supabase MCP server (`internal/mcp/servers/supabase/`) -- entirely excised
- Vercel deploy backend (`internal/tunnel/actions/deploy/vercel.go`)
- Antspace deploy backend (`internal/tunnel/actions/deploy/antspace.go`)
- Baku project features (initialization, templates, settings bootstrap)
- Related auth fields (Supabase credentials, Vercel token, Antspace URL/token)

**Added in 64bc4dc1:**

- `filestore_url` and `filesystem_id` JSON fields (new deploy mechanism)
- `jwt` JSON field (auth-related)

**Unchanged:** V0/V1 session context structs, CLI flags, sandbox settings, API endpoints.

See `64bc4dc1/BINDIFF_RESULTS.md` for full analysis and `64bc4dc1/PLAN.md` for
detailed status.
