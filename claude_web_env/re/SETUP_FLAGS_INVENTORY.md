# Setup Flags Inventory — Proprietary Binaries

Complete reverse engineering of all CLI flags, environment variables, and
configuration options for both proprietary Claude Code web container binaries.
Covers when and how each flag is consumed during startup and session lifecycle.

**Source of truth**: `--help` output from the live binaries (Build IDs below).
Reconstructed source provides call-path details.

---

## Binary 1: `process_api` (Rust, stripped)

**Build ID**: `e409c31a846219e05541706c43daf1756365f486`
**Framework**: `clap 4.5.20` (derive macro, `#[derive(Parser)]`)
**Env var convention**: Each `--flag-name` has a corresponding `SCREAMING_SNAKE`
env var via `#[arg(env = "...")]`.

### CLI Flags

| Flag                           | Env Var                   | Type             | Default | Required     | Description                                                                              |
| ------------------------------ | ------------------------- | ---------------- | ------- | ------------ | ---------------------------------------------------------------------------------------- |
| `--addr <ADDR>`                | `ADDR`                    | `Option<String>` | None    | Conditional† | WebSocket listen address (e.g., `0.0.0.0:2024`)                                          |
| `--max-ws-buffer-size <SIZE>`  | `MAX_WS_BUFFER_SIZE`      | `usize`          | `32768` | No           | WebSocket frame buffer size in bytes                                                     |
| `--memory-limit-bytes <BYTES>` | `MEMORY_LIMIT_BYTES`      | `Option<u64>`    | None    | No           | Container-level memory limit; enables OOM monitor                                        |
| `--cpu-shares <SHARES>`        | `CPU_SHARES`              | `Option<u64>`    | None    | No           | CPU weight — cgroup v1 `cpu.shares` or v2 `cpu.weight`                                   |
| `--oom-polling-period-ms <MS>` | `OOM_POLLING_PERIOD_MS`   | `u64`            | `100`   | No           | OOM check interval in milliseconds                                                       |
| `--cgroupv2`                   | `CGROUPV2`                | `bool`           | `false` | No           | Force cgroup v2 mode (auto-detected otherwise)                                           |
| `--control-server-addr <ADDR>` | `CONTROL_SERVER_ADDR`     | `Option<String>` | None    | No           | HTTP control server address (e.g., `0.0.0.0:2025`). **Disables SIGINT handler** when set |
| `--block-local-connections`    | `BLOCK_LOCAL_CONNECTIONS` | `bool`           | `false` | No           | Reject connections from 127.0.0.1, ::1, 0.0.0.0, ::                                      |
| `--listen-uds <PATH>`          | `LISTEN_UDS`              | `Option<String>` | None    | No           | Unix domain socket path for WebSocket listener                                           |
| `--control-vsock-port <PORT>`  | `CONTROL_VSOCK_PORT`      | `Option<u32>`    | None    | No           | Vsock port for control server (Firecracker)                                              |
| `--listen-vsock-port <PORT>`   | `LISTEN_VSOCK_PORT`       | `Option<u32>`    | None    | No           | Vsock port for WebSocket listener (Firecracker)                                          |
| `--firecracker-init`           | `FIRECRACKER_INIT`        | `bool`           | `false` | No           | Run as Firecracker VM init (PID 1). Full VM bootstrap before services start              |
| `-h, --help`                   | —                         | —                | —       | —            | Print help                                                                               |

†`--addr` is required unless `--listen-uds` or `--listen-vsock-port` is provided.

### Flag Consumption Call Paths

#### `--firecracker-init`

**When**: Checked immediately after CLI parse, **before** the async runtime starts.
**Path**: `main()` → `if cli.firecracker_init` → `firecracker_init::run_firecracker_init()`

Full init sequence (synchronous, blocking):

1. Mount essential filesystems (`/proc`, `/sys`, `/dev`, `/dev/pts`, `/dev/shm`, cgroup2)
2. Set up networking (IP=192.0.2.2/24, GW=192.0.2.1, MTU=1400)
3. Mount root filesystem from `/dev/vda` (ext4 or squashfs)
4. `pivot_root` (or `MS_MOVE+chroot` fallback)
5. Mount model tools from `/dev/vdb` (squashfs)
6. Create `/dev/fuse`, spawn rclone for FUSE mounts
7. Mount rclone_tools from block device
8. Mount readonly block devices
9. Write config files (`etc_hosts`, `resolv_conf`, `ca_cert_pem`)
10. Load environment variables from `/container.env` (JSON)
11. Scrub auth tokens from saved configs
12. Set system clock via `clock_settime`
13. Drop `CAP_SYS_RESOURCE`
14. Write `/proc/sys/vm/drop_caches`

**Snapstart mode**: If `/mount_config.json` doesn't exist at boot, enters snapstart
template mode and signals `SNAPSTART_READY`. The mount config is then supplied via
`POST /mount_root` on the control server.

Also sets `mount_root_enabled = true` on the control server, enabling the
`POST /mount_root` endpoint.

#### `--addr` / `--listen-uds` / `--listen-vsock-port`

**When**: After cgroup setup, after control server start.
**Path**: `main()` → priority resolution:

1. `--listen-vsock-port` → `run_vsock_ws_listener()` (binds `VsockListener`, validates CID==2)
2. `--listen-uds` → `run_uds_ws_listener()` (binds `UnixListener`, sets `0o777` perms)
3. `--addr` → `TcpListener::bind()` (standard TCP WebSocket)
4. None → error: "No listener configured"

All three paths feed connections into the same `io::handle_ws_connection()`.

#### `--control-server-addr` / `--control-vsock-port`

**When**: After cgroup setup, before WebSocket listener.
**Path**: `main()` →

- TCP: `control_server::start_control_server(addr, ..., mount_root_enabled)`
- Vsock: `control_server::start_vsock_control_server(port, ..., mount_root_enabled)`
- Neither: SIGINT handler enabled instead

**Mutual exclusion**: When a control server is configured, SIGINT handler is disabled.
Shutdown is driven by `POST /shutdown` on the control server.

#### `--memory-limit-bytes`

**When**: After cgroup setup, spawns container OOM monitor task.
**Path**: `main()` → `if let Some(memory_limit) = cli.memory_limit_bytes` →
`tokio::spawn(oom_killer::container_oom_monitor(...))`

Also passed to every `io::handle_ws_connection()` for per-connection context.

#### `--oom-polling-period-ms`

**When**: Converted to `Duration` and passed to OOM monitor and all WS connection handlers.
**Path**: `Duration::from_millis(cli.oom_polling_period_ms)` → both
`container_oom_monitor()` and `handle_ws_connection()`.

#### `--cpu-shares`

**When**: After cgroup setup.
**Path**: `main()` → `cgroup::set_cpu_shares(&controller.base_path, controller.version, shares)`

#### `--cgroupv2`

**When**: During cgroup setup.
**Path**: `main()` → `cgroup::setup_cgroup(cli.cgroupv2)` — forces v2 path regardless
of auto-detection.

#### `--block-local-connections`

**When**: During WebSocket accept loop.
**Path**: For TCP: checks `is_local_ip(&remote_addr.ip())` on each connection.
For vsock: checks `peer_cid != 2` (host-only).
Control server: **always** rejects local IPs regardless of this flag.

### Live Container Invocation

```
/process_api --firecracker-init \
  --addr 0.0.0.0:2024 \
  --max-ws-buffer-size 32768 \
  --block-local-connections
```

---

## Binary 2: `environment-manager` (Go, unstripped)

**Build ID**: `a6f96673c2497a946dc0797780b5c6df47c0946e`
**Version**: `staging-68f0dff496` (via `-ldflags -X main.Version`)
**Framework**: `cobra v1.9.1` + `pflag`
**Binary name**: `environment-manager` on disk, `environment-runner` as CLI name

### Global Flags

| Flag            | Type | Default | Description                    |
| --------------- | ---- | ------- | ------------------------------ |
| `-h, --help`    | bool | —       | Help for environment-runner    |
| `-v, --version` | bool | —       | Version for environment-runner |

### Subcommand: `setup`

Pre-installs dependencies for orchestrator mode. Does NOT start a session.

| Flag                        | Type   | Default                     | Description                                                                              |
| --------------------------- | ------ | --------------------------- | ---------------------------------------------------------------------------------------- |
| `--api-url`                 | string | `https://api.anthropic.com` | API base URL for connectivity healthcheck                                                |
| `--claude-code-version`     | string | `latest`                    | Version to install (`latest`\|`stable`\|`X.Y.Z`)                                         |
| `--log-level`               | string | `info`                      | Log level (`debug`\|`info`\|`warn`\|`error`)                                             |
| `--sandbox-runtime-version` | string | `latest`                    | Version to install (`latest`\|`X.Y.Z`)                                                   |
| `--service-key-file`        | string | `""`                        | Path to service key for API healthcheck. Falls back to `ENVIRONMENT_SERVICE_KEY` env var |
| `--skip-claude-code`        | bool   | `false`                     | Skip Claude Code installation                                                            |
| `--skip-sandbox-runtime`    | bool   | `false`                     | Skip sandbox-runtime installation                                                        |

**Call path**: `AddSetupCommand()` → `runSetup()`:

1. `parseLogLevel(logLevel)` → `logger.CreateLoggerWithFileOutput(level)`
2. If not both skipped: `runPreflightChecks()` → `checkNpmAvailable()` (runs `npm --version`)
3. `loadServiceKey(serviceKeyFile)` — reads file, falls back to `ENVIRONMENT_SERVICE_KEY` env var
4. If service key: `runAPIHealthcheck()` → `orchestrator.NewWhoamiClient()` → `GetIdentity()` → calls `/v1/environments/whoami`
5. If `!skipClaudeCode`: `installClaudeCode()` → `claude.InstallOrUpdateClaudeCode()` → `npm install -g @anthropic-ai/claude-code@<version>`
6. If `!skipSandboxRuntime`: `installSandboxRuntime()` → `sandbox.InstallSandboxRuntime()` → `npm install -g @anthropic-ai/sandbox-runtime@<version>`

### Subcommand: `orchestrator`

Primary production entry point. Full session lifecycle.

| Flag                      | Type     | Default                     | Description                                                                                   |
| ------------------------- | -------- | --------------------------- | --------------------------------------------------------------------------------------------- |
| `--api-url`               | string   | `https://api.anthropic.com` | API base URL                                                                                  |
| `--client-id`             | string   | hostname                    | Client/worker identifier                                                                      |
| `--environment-id`        | string   | `""`                        | Environment ID (validated against whoami)                                                     |
| `--execute-hook`          | string   | `""`                        | Custom session handler command. If not set, self-invokes `task-run --stdin --input-format=v1` |
| `--execute-hook-timeout`  | duration | `0`                         | Timeout for execute hook (0 = no timeout)                                                     |
| `--log-level`             | string   | `info`                      | Log level                                                                                     |
| `--loop-timeout`          | duration | `5m`                        | Loop timeout before triggering timeout hook                                                   |
| `--max-poll-failures`     | int      | `0`                         | Max consecutive failures before exit (0 = infinite)                                           |
| `--organization-id`       | string   | `""`                        | Organization ID (validated against whoami)                                                    |
| `--poll-hook`             | string   | `""`                        | Custom polling command (replaces built-in Poller)                                             |
| `--poll-hook-timeout`     | duration | `30s`                       | Poll hook timeout                                                                             |
| `--poll-timeout`          | duration | `5m`                        | Poll request timeout                                                                          |
| `--reclaim-older-than-ms` | int      | `0`                         | Reclaim unacknowledged work items (0 = API default 5000ms)                                    |
| `--sandbox-backend`       | string   | `sandbox-runtime`           | `sandbox-runtime` (default) or `none`                                                         |
| `--sandbox-settings`      | string   | `""`                        | Path to custom sandbox-runtime settings JSON                                                  |
| `--service-key-file`      | string   | `""`                        | Path to service key file. Falls back to `ENVIRONMENT_SERVICE_KEY`                             |
| `--skip-container-lock`   | bool     | `false`                     | Skip container-level lock (dev/testing only)                                                  |
| `--skip-git-config`       | bool     | `false`                     | Skip git configuration setup                                                                  |
| `--timeout-hook`          | string   | `""`                        | Command to run on loop timeout                                                                |
| `--timeout-hook-timeout`  | duration | `5m`                        | Timeout hook timeout                                                                          |

**Call path**: `AddOrchestratorCommand()` → `RunE` closure:

1. Parse log level, create logger
2. Read service key from `--service-key-file` or `ENVIRONMENT_SERVICE_KEY`
3. `orchestrator.NewWhoamiClient()` → `GetIdentity()` — discovers env/org ID
4. Validate `--environment-id` / `--organization-id` against whoami response
5. Create poller: `--poll-hook` → `NewPollHook()`, otherwise → `NewPollerWithWorkerID()`
6. If `--sandbox-backend` set: `sandbox.InstallSandboxRuntime()`
7. Acquire container lock (unless `--skip-container-lock`)
8. `orchestrator.NewOrchestrator(poller, ...)` → `orch.Run(ctx)`
9. Orchestrator loop: poll → receive work → dispatch execute hook (or self-invoke `task-run`) → report results → repeat
10. Graceful shutdown on SIGTERM/SIGINT

### Subcommand: `task-run`

Executes a single session task received via stdin.

| Flag                           | Type   | Default | Description                                             |
| ------------------------------ | ------ | ------- | ------------------------------------------------------- |
| `--allowed-tools`              | string | `""`    | Comma-separated allowed tools for Claude                |
| `--claude-agent-version`       | string | `""`    | Target version (`latest`\|`current`\|`stable`\|`X.Y.Z`) |
| `--claude-path`                | string | `""`    | Path to Claude CLI executable or wrapper                |
| `--debug`                      | bool   | `false` | Enable debug mode when executing Claude Agent           |
| `--environment-id`             | string | `""`    | Environment ID (required for self-hosted)               |
| `--input-format`               | string | `v0`    | Input format: `v0` (legacy) or `v1` (work response)     |
| `--local-append-system-prompt` | string | `""`    | Additional system prompt to append locally              |
| `--local-testing`              | bool   | `false` | Disable WebSocket connections and git config            |
| `--log-level`                  | string | `info`  | Log level                                               |
| `--organization-id`            | string | `""`    | Organization ID (required for self-hosted)              |
| `--print-code-logs`            | bool   | `false` | Print Claude Code logs on completion/failure            |
| `--session`                    | string | `""`    | Session ID (required)                                   |
| `--session-mode`               | string | `new`   | `new`\|`resume`\|`resume-cached`\|`setup-only`          |
| `--skip-git-config`            | bool   | `false` | Skip git configuration setup                            |
| `--stdin`                      | bool   | —       | **Deprecated**: stdin is always used                    |
| `--upgrade-claude-code`        | bool   | `true`  | **Deprecated**: use `--claude-agent-version`            |
| `--verbose-claude-logs`        | bool   | `false` | Enable verbose Claude Agent output                      |
| `--working-directory`          | string | `/root` | Default working directory                               |

**Call path**: `AddTaskRunCommand()` → `RunE` closure:

1. Set env vars: `CLAUDE_CODE_SESSION_ID`, `CLAUDE_CODE_BASE_REF`
2. Parse log level, create logger
3. Acquire container lock
4. `config.ParseSessionMode(mode)`
5. `loadContextFromStdin()` — reads all stdin, selects V0/V1 parser, returns `ParsedContext`
6. Get `ENVIRONMENT_SERVICE_KEY` from env
7. `acknowledgeWorkIfNeeded()` — sends work ACK to API (30s timeout)
8. Check `ENVRUNNER_SKIP_CLAUDE_CODE` env var
9. If not skipped: `claude.InstallOrUpdateClaudeCode()` (10 min timeout)
10. `initDiagLogging()` — diagnostic log service with session ingress
11. Initialize OpenTelemetry from `OTEL_EXPORTER_OTLP_ENDPOINT`
12. Validate session config
13. Create `manager.Manager{}` → `mgr.Run()`
14. Manager handles: environment type init, source cloning, git proxy, MCP servers, tunnel registration
15. Create `claude.NewClaudeCodeExecutor()` → `executor.Execute()`

### Subcommand: `poll`

Single poll request for work.

| Flag                      | Type   | Default                     | Description                                                      |
| ------------------------- | ------ | --------------------------- | ---------------------------------------------------------------- |
| `--api-url`               | string | `https://api.anthropic.com` | API base URL                                                     |
| `--environment-id`        | string | `""`                        | Environment ID                                                   |
| `--log-level`             | string | `info`                      | Log level                                                        |
| `--organization-id`       | string | `""`                        | Organization ID                                                  |
| `--reclaim-older-than-ms` | int    | `0`                         | Reclaim unacknowledged work (0 = API default 5000ms)             |
| `--service-key-file`      | string | `""`                        | Path to service key file                                         |
| `--worker-id`             | string | hostname                    | Unique worker identifier (sent via `Anthropic-Worker-ID` header) |

### Subcommand: `print-sandbox-settings`

Outputs default sandbox-runtime settings as JSON to stdout. No flags beyond `--help`.

---

## Environment Variables (Read at Runtime)

### `process_api`

All flags double as env vars (clap `env` attribute):

| Variable                  | Maps to Flag                |
| ------------------------- | --------------------------- |
| `ADDR`                    | `--addr`                    |
| `MAX_WS_BUFFER_SIZE`      | `--max-ws-buffer-size`      |
| `MEMORY_LIMIT_BYTES`      | `--memory-limit-bytes`      |
| `CPU_SHARES`              | `--cpu-shares`              |
| `OOM_POLLING_PERIOD_MS`   | `--oom-polling-period-ms`   |
| `CGROUPV2`                | `--cgroupv2`                |
| `CONTROL_SERVER_ADDR`     | `--control-server-addr`     |
| `BLOCK_LOCAL_CONNECTIONS` | `--block-local-connections` |
| `LISTEN_UDS`              | `--listen-uds`              |
| `CONTROL_VSOCK_PORT`      | `--control-vsock-port`      |
| `LISTEN_VSOCK_PORT`       | `--listen-vsock-port`       |
| `FIRECRACKER_INIT`        | `--firecracker-init`        |

### `environment-manager`

Env vars read directly via `os.Getenv()` (not flag-backed):

| Variable                                       | Read By                                     | Purpose                                                             |
| ---------------------------------------------- | ------------------------------------------- | ------------------------------------------------------------------- |
| `ENVIRONMENT_SERVICE_KEY`                      | `setup`, `orchestrator`, `task-run`, `poll` | API authentication key (fallback when `--service-key-file` not set) |
| `CLAUDE_DEFAULT_PATH`                          | `setup` → `installClaudeCode()`             | Override default Claude binary name (default: `claude`)             |
| `ENVRUNNER_SKIP_CLAUDE_CODE`                   | `task-run`                                  | Skip Claude Code install when `"true"`                              |
| `SKIP_GIT_CONFIG`                              | `task-run` → manager                        | Skip git configuration setup                                        |
| `OTEL_EXPORTER_OTLP_ENDPOINT`                  | `task-run`                                  | OpenTelemetry OTLP endpoint for telemetry                           |
| `CLAUDE_CODE_SESSION_ID`                       | `task-run` (sets it)                        | Session ID propagated to Claude Code process                        |
| `CLAUDE_CODE_BASE_REF`                         | `task-run` (sets it)                        | Base ref propagated to Claude Code process                          |
| `CLAUDE_CODE_REMOTE`                           | Claude executor (sets `=true`)              | Tells Claude Code it's running in remote mode                       |
| `CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR`          | Claude executor                             | File descriptor for API key injection                               |
| `CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`      | Claude executor                             | File descriptor for OAuth token injection                           |
| `CLAUDE_CODE_WEBSOCKET_AUTH_FILE_DESCRIPTOR`   | Claude executor                             | File descriptor for WebSocket auth                                  |
| `CLAUDE_CODE_REMOTE_SESSION_ID`                | Claude executor                             | Remote session ID for Claude Code                                   |
| `CLAUDE_CODE_ENVIRONMENT_RUNNER_VERSION`       | Claude executor                             | Version string sent to Claude Code                                  |
| `CLAUDE_CODE_ENTRYPOINT`                       | Claude executor                             | Entrypoint configuration                                            |
| `CLAUDE_CODE_DEBUG`                            | Claude executor                             | Debug mode flag                                                     |
| `CLAUDE_CODE_USE_CCR_V2`                       | Claude executor                             | CCR v2 protocol flag                                                |
| `CLAUDE_CODE_WORKER_EPOCH`                     | Claude executor                             | Worker epoch for session tracking                                   |
| `CLAUDE_CODE_STUCK_THRESHOLD_SECONDS`          | Claude executor                             | Timeout for stuck detection                                         |
| `CLAUDE_CODE_EXIT_AFTER_STOP_DELAY`            | Claude executor                             | Delay before exit after stop                                        |
| `CLAUDE_CODE_DIAGNOSTICS_FILE`                 | Claude executor                             | Diagnostics output file path                                        |
| `CLAUDE_ENV_FILE`                              | Claude config                               | Claude environment file path                                        |
| `CLAUDE_PROJECT_DIR`                           | Claude config                               | Project directory override                                          |
| `CLAUDE_SESSION_INGRESS_TOKEN_FILE`            | Claude executor                             | Session ingress token file path                                     |
| `CODESIGN_MCP_PORT`                            | MCP code-sign server                        | Port for code-sign MCP server                                       |
| `CODESIGN_MCP_TOKEN`                           | MCP code-sign server                        | Auth token for MCP server                                           |
| `SANDBOX_RUNTIME_TARGET_VERSION`               | Sandbox install                             | Override sandbox-runtime version                                    |
| `SRT_BINARY_PATH`                              | Sandbox runtime                             | Path to sandbox-runtime binary                                      |
| `TUNNEL_ENABLED`                               | Tunnel client                               | Enable/disable tunnel system                                        |
| `SKIP_PLUGIN_MARKETPLACE`                      | Manager                                     | Skip plugin marketplace registration                                |
| `SUPABASE_MCP_DISABLED`                        | Manager                                     | Disable Supabase MCP integration                                    |
| `BAKU_SUPABASE_LAZY`                           | Manager                                     | Lazy Supabase initialization                                        |
| `POST_CLONE_HOOK_PATH`                         | Sources/git                                 | Path to post-clone hook script                                      |
| `NODE_DIR`                                     | Anthropic env type                          | Node.js installation directory                                      |
| `NODE_PATH`                                    | Anthropic env type                          | Node.js module path                                                 |
| `PYTHONPATH`                                   | Anthropic env type                          | Python module path                                                  |
| `HTTP_PROXY` / `HTTPS_PROXY` / `NO_PROXY`      | HTTP clients                                | Standard proxy configuration                                        |
| `GIT_TRACE_PACKET`                             | Git proxy                                   | Git packet trace debugging                                          |
| `OTEL_SERVICE_NAME`                            | O11y                                        | OpenTelemetry service name                                          |
| `OTEL_RESOURCE_ATTRIBUTES`                     | O11y                                        | OpenTelemetry resource attributes                                   |
| Various `OTEL_*`                               | O11y                                        | Full OTLP configuration suite                                       |
| `VITE_SUPABASE_URL` / `VITE_SUPABASE_ANON_KEY` | Supabase/Baku                               | Supabase project configuration                                      |

---

## Boot Sequence: How Flags Flow Through the System

### 1. Firecracker VM Boot

```
firecracker → /process_api --firecracker-init --addr 0.0.0.0:2024 ...
  ├─ [sync] firecracker_init::run_firecracker_init()
  │    ├─ mount /proc, /sys, /dev, cgroup2
  │    ├─ network: IP=192.0.2.2/24, GW=192.0.2.1
  │    ├─ mount /dev/vda root → pivot_root
  │    ├─ mount /dev/vdb model_tools
  │    ├─ FUSE mounts (rclone)
  │    ├─ load /container.env → env vars + memory/filestore config
  │    ├─ clock_settime, drop CAP_SYS_RESOURCE
  │    └─ return → async runtime starts
  ├─ [async] cgroup::setup_cgroup(false)
  ├─ [async] control_server (if --control-server-addr or --control-vsock-port)
  ├─ [async] adopter::monitor_orphans()
  ├─ [async] oom_killer::container_oom_monitor() (if --memory-limit-bytes)
  └─ [async] WebSocket accept loop (--addr / --listen-uds / --listen-vsock-port)
```

### 2. Environment Manager Launch

```
process_api WS → environment-manager orchestrator
  ├─ parseLogLevel → CreateLoggerWithFileOutput
  ├─ read ENVIRONMENT_SERVICE_KEY (file or env)
  ├─ whoami → discover environment_id, organization_id
  ├─ create poller (PollHook or Poller)
  ├─ install sandbox runtime (if --sandbox-backend set)
  ├─ acquire container lock
  ├─ NewOrchestrator → Run(ctx)
  │    └─ loop:
  │         ├─ poll for work
  │         ├─ receive session task
  │         ├─ dispatch: --execute-hook OR self-invoke task-run
  │         ├─ report results
  │         └─ sleep with jitter if queue empty
  └─ graceful shutdown on SIGTERM/SIGINT
```

### 3. Task Execution

```
environment-manager task-run --session=<id> --input-format=v1
  ├─ set CLAUDE_CODE_SESSION_ID, CLAUDE_CODE_BASE_REF
  ├─ parseLogLevel → CreateLoggerWithFileOutput
  ├─ acquire container lock
  ├─ loadContextFromStdin() → V0/V1 parser
  ├─ acknowledgeWorkIfNeeded() → POST /work/{wid}/ack
  ├─ claude.InstallOrUpdateClaudeCode() (unless ENVRUNNER_SKIP_CLAUDE_CODE=true)
  ├─ initDiagLogging() → session ingress client
  ├─ init OpenTelemetry (OTEL_EXPORTER_OTLP_ENDPOINT)
  ├─ manager.Manager.Run()
  │    ├─ environment type init (anthropic or byoc)
  │    ├─ source processing (git clone/fetch)
  │    ├─ git proxy setup
  │    ├─ MCP server registration
  │    ├─ tunnel registration
  │    └─ language runtime setup
  └─ claude.NewClaudeCodeExecutor().Execute()
       ├─ spawn via process_api WebSocket (port 2024)
       ├─ inject API key via file descriptor
       ├─ set CLAUDE_CODE_REMOTE=true + all CLAUDE_CODE_* env vars
       └─ stream I/O until completion
```

---

## RE Source vs Binary Discrepancies

The reconstructed source in `a6f96673/src/cmd/` has **stale flag names** that
don't match the live binary's `--help` output. The binary is ground truth.

| RE Source Flag             | Actual Binary Flag       | Notes                           |
| -------------------------- | ------------------------ | ------------------------------- |
| `--secret-path`            | `--service-key-file`     | Renamed                         |
| `--session-id`             | `--environment-id`       | Renamed (orchestrator)          |
| `--hook-command`           | `--execute-hook`         | Renamed                         |
| `--hook-timeout`           | `--execute-hook-timeout` | Renamed                         |
| `--session-timeout`        | `--loop-timeout`         | Renamed                         |
| `--sandbox-enabled`        | (removed)                | Replaced by `--sandbox-backend` |
| `--task-command`           | `--execute-hook`         | Consolidated                    |
| `--work-id` (orchestrator) | `--client-id`            | Renamed                         |

The RE source was reconstructed from an earlier analysis pass. The flag names,
descriptions, and defaults in this document are from the **live binary**.
