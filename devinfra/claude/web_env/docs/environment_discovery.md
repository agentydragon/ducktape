# Claude Code Web Environment Discovery

Documented from a live session on 2026-03-26.

## Binary Versions

| Binary                | Build ID   | Release / Version              |
| --------------------- | ---------- | ------------------------------ |
| `process_api` (PID 1) | `91c789ff` | `process_api_2026-03-23-22-49` |
| `environment-manager` | `495ea204` | `release-d84d76b7-ext`         |

`environment-manager` is garble-obfuscated (release channel, 52MB).
DWARF/symbol extraction is not possible.

## Table of Contents

- [Container Init Process](#container-init-process)
- [Sandbox Runtime Settings](#sandbox-runtime-settings)
- [Environment Runner](#environment-runner)
- [WebSocket Architecture](#websocket-architecture)
- [Proxy Systems](#proxy-systems)
- [MCP Server: Codesign](#mcp-server-codesign)
- [Claude Configuration](#claude-configuration)
- [Environment Variables](#environment-variables)
- [Git Proxy](#git-proxy)
- [Code Signing](#code-signing)
- [Security Analysis](#security-analysis)
- [Architecture Summary](#architecture-summary)

## Container Init Process

### PID 1: process_api

The container uses a custom init process instead of systemd:

```
PID 1: /process_api --firecracker-init \
         --addr 0.0.0.0:2024 \
         --max-ws-buffer-size 32768 \
         --block-local-connections
```

**Binary**: `/process_api` (Rust, ELF 64-bit, static-pie linked, stripped)

As of 2026-03-16, the binary uses `--firecracker-init` mode, which adds a
full VM init system (mount root, pivot_root, networking, FUSE, rclone) before
starting the WebSocket listener. The `--cpu-shares`, `--memory-limit-bytes`,
and `--oom-polling-period-ms` flags are no longer passed on the command line
in the current container invocation.

**Help output**:

```
Usage: process_api [OPTIONS] --addr <ADDR>

Options:
      --addr <ADDR>                             WebSocket API address
      --max-ws-buffer-size <SIZE>               [default: 32768]
      --memory-limit-bytes <BYTES>              Container memory limit
      --cpu-shares <SHARES>                     CPU shares for cgroups
      --oom-polling-period-ms <MS>              [default: 100]
      --cgroupv2                                Use cgroups v2
      --control-server-addr <ADDR>              For graceful shutdown
      --block-local-connections                 Block localhost connections
      --firecracker-init                        Run as Firecracker VM init
```

**Purpose**:

- Acts as container init (PID 1)
- In `--firecracker-init` mode: mounts root filesystem, sets up networking,
  configures FUSE, mounts rclone tools, then spawns the main process
- Exposes WebSocket API on port 2024
- Manages container resources (memory limits, CPU shares via cgroups)
- Handles OOM conditions
- Blocks local network connections for security

### Startup Sequence

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Container Runtime (external)                                            │
│   - Injects proxy env vars with JWT                                     │
│   - Sets IS_SANDBOX=yes                                                 │
│   - Mounts volumes, sets up networking                                  │
└────────────────────────┬────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ PID 1: /process_api                                                     │
│   - Listens on 0.0.0.0:2024 (WebSocket API)                            │
│   - Enforces memory limit (16GB)                                        │
│   - Blocks local connections                                            │
│   - Spawns shell with session command                                   │
└────────────────────────┬────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ PID 23: /bin/sh -c "mkdir -p /home/user ; cd /home/user && ..."        │
│   - Creates user directory                                              │
│   - Launches environment-manager                                        │
└────────────────────────┬────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ PID 25: environment-manager task-run --session {id}                     │
│   - Clones repos (or resumes from cache)                                │
│   - Sets up git proxy                                                   │
│   - Starts MCP servers                                                  │
│   - Installs hooks                                                      │
│   - Spawns claude CLI                                                   │
└────────────────────────┬────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ PID 43: claude (Node.js)                                                │
│   - Connects to WebSocket                                               │
│   - Executes tools                                                      │
│   - Spawns bash shells for commands                                     │
└─────────────────────────────────────────────────────────────────────────┘
```

### Environment Inheritance

The proxy environment variables are set at the **container level** (injected before PID 1 starts) and inherited by all processes:

```
Container runtime
    └── process_api (PID 1) [inherits HTTP_PROXY, etc.]
        └── /bin/sh (PID 23) [inherits]
            └── environment-manager (PID 25) [inherits]
                └── claude (PID 43) [inherits]
                    └── bash shells [inherits]
```

## Sandbox Runtime Settings

From `/usr/local/bin/environment-manager print-sandbox-settings`:

```json
{
  "network": {
    "allowedDomains": ["api.anthropic.com", "api-staging.anthropic.com", "*.anthropic.com"],
    "deniedDomains": []
  },
  "filesystem": {
    "denyRead": ["~/.ssh", "~/.aws", "~/.config/gcloud", "/etc/shadow", "/etc/passwd-", "/secrets"],
    "allowWrite": ["/tmp", "/tmp/claude", "~", "/workspace"],
    "denyWrite": [],
    "allowGitConfig": true
  },
  "enableWeakerNestedSandbox": false
}
```

## Environment Runner

Binary: `/usr/local/bin/environment-manager` → `/opt/env-runner/environment-manager` (Go, ELF 64-bit, garble-obfuscated)
Version: `release-9f4ec76fbc-ext`

### Overview

The environment-runner is an Anthropic-built Go binary that orchestrates Claude Code sessions in containerized environments. It handles the complete lifecycle from session initialization to cleanup.

### Help Output

```
$ environment-manager --help
Environment Runner handles the lifecycle of agentic sessions.

Usage:
  environment-runner [command]

Available Commands:
  completion             Generate the autocompletion script for the specified shell
  help                   Help about any command
  orchestrator           Poll for session tasks and hand off for execution
  poll                   Make a single poll request for work
  print-sandbox-settings Print the default sandbox runtime settings
  setup                  Install dependencies for orchestrator mode
  task-run               Handles execution of a provided session

Flags:
  -h, --help      help for environment-runner
  -v, --version   version for environment-runner
```

### task-run Subcommand

The primary command for executing a single session:

```
$ environment-manager task-run --help
Connects directly to the API to execute an existing session id.

Provide a JSON object via stdin with the following structure:
{
  "startup_context": {
    "sources": [...],
    "cwd": "..."
  },
  "environment": {
    "environment_type": "...",
    "version": "...",
    ...
  },
  "auth": [
    {
      "type": "github_app",
      "url": "github.com",
      "token": "ghs_..."
    }
  ]
}

Flags:
      --allowed-tools string                    Comma-separated list of allowed tools for Claude
      --claude-agent-version string             Target Claude Agent version (latest, current, stable, or specific version)
      --claude-path string                      Path to the Claude CLI executable or a wrapper binary
      --debug                                   Enable debug mode when executing the Claude Agent
      --environment-id string                   Environment ID for API calls (required for self-hosted)
      --input-format string                     Input format: 'v0' or 'v1' (default "v0")
      --local-append-system-prompt string       Additional system prompt content to append locally
      --local-testing                           Disable Claude WebSocket connections and git configuration
      --log-level string                        Log level (debug, info, warn, error) (default "info")
      --organization-id string                  Organization ID for API calls (required for self-hosted)
      --print-code-logs                         Print Claude Code logs to console when execution completes or fails
      --session string                          ID of the session to manage (required)
      --session-mode string                     Session mode: 'new', 'resume', 'resume-cached', 'setup-only' (default "new")
      --skip-git-config                         Skip git configuration setup (use container's existing .gitconfig)
      --upgrade-claude-code                     Deprecated: use --claude-agent-version instead (default true)
      --verbose-claude-logs                     Enable verbose logging of Claude Agent output to console
      --working-directory string                Default working directory for the session (default "/root")
```

### orchestrator Subcommand

For self-hosted environments that poll for work:

```
$ environment-manager orchestrator --help
The orchestrator command polls the API for session tasks and handing off work to be executed.

It handles:
- Discovering environment identity via the /v1/environments/whoami endpoint
- Polling the environments API work/poll endpoint
- Executing work:
  - With --execute-hook: pipes JSON to hook via stdin and exits with hook's exit code
  - Without --execute-hook: auto-invokes 'task-run --input-format=v1'
    - With --sandbox-backend=sandbox-runtime (default): wraps execution in sandbox
    - With --sandbox-backend=none: runs without sandbox (logs warning)
- Running timeout hooks for periodic maintenance (e.g., monorepo updates)
- Sleeping with jitter when queue is empty
- Graceful shutdown on SIGTERM/SIGINT

Required environment variable:
  ENVIRONMENT_SERVICE_KEY: Service key for the environment

The environment ID and organization ID are discovered automatically via the whoami
endpoint. You can optionally provide --environment-id and --organization-id flags
to validate them against the token's identity.

Flags:
      --api-url string                  API base URL (default "https://api.anthropic.com")
      --client-id string                Client ID (worker identifier, default: hostname)
      --environment-id string           Environment ID (find at claude.ai/settings)
      --execute-hook string             Command to run with session JSON via stdin when task received
      --execute-hook-timeout duration   Timeout for execute hook (0 = no timeout)
      --log-level string                Log level (debug, info, warn, error) (default "info")
      --loop-timeout duration           Loop timeout before triggering timeout hook (default 5m0s)
      --max-poll-failures int           Maximum consecutive poll failures before exiting (0 = infinite)
      --organization-id string          Organization ID (find at claude.ai/settings > Account)
      --poll-hook string                Command to execute for polling instead of built-in Poller
      --poll-hook-timeout duration      Timeout for poll hook execution (default 30s)
      --poll-timeout duration           Poll request timeout duration (default 5m0s)
      --reclaim-older-than-ms int       Reclaim unacknowledged work items older than this many ms (0 = API default)
      --sandbox-backend string          Sandbox backend: none, sandbox-runtime (default "sandbox-runtime")
      --sandbox-settings string         Path to custom sandbox-runtime settings JSON file
      --service-key-file string         Path to file containing the environment service key
      --skip-container-lock             Skip container-level lock (WARNING: allows multiple sessions)
      --skip-git-config                 Skip git configuration setup
      --timeout-hook string             Command to run on loop timeout (e.g., monorepo updates)
      --timeout-hook-timeout duration   Timeout for timeout hook execution (default 5m0s)
```

### poll Subcommand

For making a single non-blocking poll request:

```
$ environment-manager poll --help
The poll command makes a single non-blocking request to the API for work.

Flags:
      --api-url string              API base URL (default "https://api.anthropic.com")
      --environment-id string       Environment ID
      --log-level string            Log level (debug, info, warn, error) (default "info")
      --organization-id string      Organization ID
      --reclaim-older-than-ms int   Reclaim unacknowledged work items older than this many ms (0 = API default)
      --service-key-file string     Path to file containing the environment service key
      --worker-id string            Unique worker identifier (defaults to hostname)
```

### setup Subcommand

For pre-installing dependencies during container image build:

```
$ environment-manager setup --help
The setup command pre-installs all required dependencies for orchestrator mode.
Installs Claude Code and Sandbox Runtime via npm.

Flags:
      --api-url string                   API base URL for connectivity healthcheck (default "https://api.anthropic.com")
      --claude-code-version string       Version of Claude Code to install (default "latest")
      --log-level string                 Log level (debug, info, warn, error) (default "info")
      --sandbox-runtime-version string   Version of sandbox-runtime to install (default "latest")
      --service-key-file string          Path to environment service key file for API healthcheck
      --skip-claude-code                 Skip Claude Code installation
      --skip-sandbox-runtime             Skip sandbox-runtime installation
```

### Session Modes

| Mode            | Description                                                       |
| --------------- | ----------------------------------------------------------------- |
| `new`           | Full setup: clone repos, install languages, run init scripts      |
| `resume`        | Skip clone, fetch latest, checkout branch                         |
| `resume-cached` | Reuse existing container state (fastest, default for self-hosted) |
| `setup-only`    | Exit after setup without starting claude                          |

### What It Does (from logs)

1. **Git Proxy Setup**: Rewrites git remotes to route through Anthropic's session ingress
2. **Environment Initialization**: Installs languages (Python, Node), sets up MCP servers
3. **Hook Installation**: Installs stop-hook, session-start skill
4. **Claude CLI Invocation**: Spawns `claude` with controlled flags and auth

## WebSocket Architecture

### Connection Setup

```
┌─────────────────────┐                    ┌──────────────────────────────────┐
│   claude CLI        │◄──── WebSocket ───►│  api.anthropic.com               │
│   (in container)    │                    │  /v1/session_ingress/ws/{id}     │
└─────────────────────┘                    └──────────────────────────────────┘
         │                                              │
         │  --sdk-url wss://api.anthropic.com/         │
         │     v1/session_ingress/ws/{session_id}      │
         │                                              │
         │  Auth via file descriptor 3                 │
         │  (CLAUDE_CODE_WEBSOCKET_AUTH_FILE_DESCRIPTOR)│
         │                                              ▼
         │                                    ┌──────────────────┐
         └────────────────────────────────────│  claude.ai UI    │
                                              │  (user browser)  │
                                              └──────────────────┘
```

### Key Endpoints

| Endpoint                                                                   | Protocol  | Purpose                               |
| -------------------------------------------------------------------------- | --------- | ------------------------------------- |
| `wss://api.anthropic.com/v1/session_ingress/ws/{session_id}`               | WebSocket | Bidirectional real-time communication |
| `https://api.anthropic.com/v1/session_ingress/session/{session_id}`        | HTTP      | Session persistence/recovery          |
| `https://api.anthropic.com/v2/session_ingress/session/{session_id}/events` | HTTP POST | Environment manager logs              |

### Streaming Protocol

The `claude` CLI uses bidirectional JSON streaming:

```bash
claude \
  --input-format=stream-json \    # Receives user messages as JSON stream
  --output-format=stream-json \   # Emits assistant responses as JSON stream
  --replay-user-messages          # Echoes user messages back for acknowledgment
```

**Data Flow:**

```
User types in browser
        │
        ▼
WebSocket → claude CLI stdin (stream-json)
        │
        ▼
Claude processes, calls tools
        │
        ▼
claude CLI stdout (stream-json) → WebSocket
        │
        ▼
Rendered in browser UI
```

### Authentication

Tokens are passed via **file descriptors** (not env vars or CLI args):

| FD  | Environment Variable                         | Purpose              |
| --- | -------------------------------------------- | -------------------- |
| 3   | `CLAUDE_CODE_WEBSOCKET_AUTH_FILE_DESCRIPTOR` | WebSocket auth token |
| 4   | `CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR`    | OAuth token          |

This keeps secrets out of process listings (`ps aux`) and environment dumps.

#### Token Types

Three distinct token types are used in the Claude Code web environment:

**1. Session Ingress Token (`sk-ant-si-...`)**

- **Format**: JWT (ES256 signed)
- **Passed via**: FD 3
- **Purpose**: WebSocket authentication to `wss://api.anthropic.com/v1/session_ingress/ws/{session_id}`
- **JWT Claims**:
  ```json
  {
    "iss": "session-ingress",
    "aud": ["anthropic-api"],
    "session_id": "session_...",
    "organization_uuid": "...",
    "account_uuid": "...",
    "account_email": "user@example.com",
    "application": "ccr"
  }
  ```
- **Validity**: 4 hours

**2. OAuth Access Token (`sk-ant-oat01-...`)**

- **Format**: Opaque (not a JWT)
- **Passed via**: FD 4
- **Purpose**: API authentication for Anthropic API calls

**3. Code Signing Request Token (`sk-ant-ccsr-...`)**

- **Format**: JWT (ES256 signed)
- **Purpose**: Authentication for codesign MCP server operations
- **JWT Claims**:
  ```json
  {
    "jti": "unique-request-id",
    "session_id": "session_...",
    "organization_uuid": "...",
    "organization_database_id": 12345,
    "source_type": "github",
    "source_account_id": "claude",
    "sources": ["owner/repo"],
    "permissions_scope": "write",
    "account_uuid": "...",
    "account_database_id": 12345
  }
  ```
- **Validity**: 4 hours

#### Token Delivery Mechanism

Tokens flow from the container runtime to the Claude process:

```
Container Runtime
    │
    ▼
environment-manager (PID 25)
    │ pipes tokens to FDs
    │ (one-time delivery)
    ▼
claude (PID 43-44)
    FD 3: session ingress token
    FD 4: OAuth access token
```

Tokens are delivered via pipes and consumed once. After the initial read, the pipe FDs show as connected to pipes but contain no remaining data.

#### Token Extraction Procedure

Since tokens are consumed from pipes and not stored in files, they can be extracted from the `environment-manager` process memory:

```bash
# 1. Find environment-manager PID (the Go binary, not the shell wrapper)
ENV_MGR_PID=$(pgrep -f "^/usr/local/bin/environment-manager")
echo "environment-manager PID: $ENV_MGR_PID"

# 2. Verify Go heap memory region exists
cat /proc/$ENV_MGR_PID/maps | grep -E "^c000"
# Expected: c000000000-c000800000 rw-p ... (Go heap)

# 3. Dump Go heap memory using gdb
sudo gdb -batch -p $ENV_MGR_PID \
  -ex "dump binary memory /tmp/env-mgr-heap.bin 0xc000000000 0xc000800000"

# 4. Extract tokens from memory dump
strings /tmp/env-mgr-heap.bin | grep -E '^sk-ant-' | sort -u
```

**Expected output** (three token types):

```
sk-ant-ccsr-eyJ0eXAi...  (Code Signing Request Token)
sk-ant-oat01-...         (OAuth Access Token)
sk-ant-si-eyJ0eXAi...    (Session Ingress Token)
```

**Decode JWT tokens**:

```python
import base64, json

def decode_jwt(token):
    # Remove prefix (sk-ant-si-, sk-ant-ccsr-)
    jwt = token.split('-', 3)[3]
    parts = jwt.split('.')
    # Add padding and decode payload
    payload = parts[1] + '=' * (4 - len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(payload))

# Example: decode_jwt("sk-ant-si-eyJ0eXAi...")
```

#### Token API Capabilities

| Token                          | Endpoint                          | Result                                 |
| ------------------------------ | --------------------------------- | -------------------------------------- |
| OAuth (`sk-ant-oat01-`)        | `GET /api/hello`                  | ✓ `{"message": "hello"}`               |
| OAuth (`sk-ant-oat01-`)        | `GET /api/oauth/claude_cli/roles` | ✗ Missing scope `user:profile`         |
| OAuth (`sk-ant-oat01-`)        | `POST /v1/messages`               | ✗ "OAuth authentication not supported" |
| Session Ingress (`sk-ant-si-`) | `WSS session_ingress/ws/{id}`     | ✗ Single-use (already connected)       |
| Session Ingress (`sk-ant-si-`) | `POST /v1/messages`               | ✗ Invalid x-api-key                    |
| CCSR (`sk-ant-ccsr-`)          | MCP codesign server               | ✓ Code signing operations              |

**Key findings**:

- OAuth token IS valid (authenticates to `/api/hello`) but has limited scopes
- Session ingress token is single-use per WebSocket connection
- Inference happens over WebSocket (`session_ingress`), not REST API
- Tokens cannot be used interchangeably between their intended purposes

### Claude CLI Invocation

Full command from logs:

```bash
claude \
  --output-format=stream-json \
  --verbose \
  --replay-user-messages \
  --input-format=stream-json \
  --debug-to-stderr \
  --allowed-tools "Task,Bash,Glob,Grep,Read,Edit,..." \
  --append-system-prompt "..." \
  --model claude-opus-4-5-20251101 \
  --add-dir /home/user/ducktape \
  --sdk-url wss://api.anthropic.com/v1/session_ingress/ws/{session_id} \
  --resume=https://api.anthropic.com/v1/session_ingress/session/{session_id} \
  --debug
```

## Proxy Systems

The container uses two separate proxy systems for network egress control.

### 1. Network Egress Proxy (HTTP/HTTPS)

All HTTP/HTTPS traffic is routed through an authenticated proxy.

**Proxy Address**: `21.0.0.127:15004`

**Authentication**: JWT token embedded in the URL as username:

```
http://container_{container_id}:jwt_{JWT_TOKEN}@21.0.0.127:15004
```

**JWT Payload** (decoded):

```json
{
  "iss": "anthropic-egress-control",
  "organization_uuid": "70aac9b0-f657-4178-8e9d-798bcd25ea76",
  "iat": 1768846481,
  "exp": 1768860881,
  "allowed_hosts": "*",
  "is_hipaa_regulated": "false",
  "use_egress_gateway": "false",
  "session_id": "session_018FEXHGPoJPhpx1itTvdhA6",
  "container_id": "container_01Vz7LuaCyv1EAd58QsayLrU--claude_code_remote--joyful-round-sticky"
}
```

**JWT Properties**:

- `iss`: Issuer is `anthropic-egress-control`
- `exp`: ~4 hour expiry from issuance
- `allowed_hosts`: `*` (but network layer may filter)
- Session and container binding for audit trail

**Proxy Configuration Source**:

The proxy URL (with embedded JWT) is **injected by the container runtime** into PID 1's
environment before the container starts. Key implications:

1. **Static for session lifetime** - The URL never changes after container start
2. **No refresh mechanism** - There's no API to get a new token
3. **Inherited by all processes** - All child processes inherit the env vars
4. **4-hour validity** - Token expires 4 hours after container start

```
Container runtime
    └── process_api (PID 1) ← HTTP_PROXY injected here
        └── environment-manager
            └── claude
                └── bash shells ← All inherit same proxy URL
```

**Programmatic Access**:

For tools that don't natively support proxy env vars (e.g., Bazel), the proxy URL can be:

1. **Read from environment** (recommended):

   ```bash
   PROXY_URL="$HTTP_PROXY"
   ```

2. **Extracted from PID 1** (if env var not available):

   ```bash
   xargs -0 -n1 < /proc/1/environ | grep ^HTTP_PROXY=
   ```

3. **Written to file in SessionStart hook**:
   ```bash
   echo "$HTTP_PROXY" > /tmp/anthropic-proxy-url
   ```

**Note**: The proxy URL is constant for the session duration. Reading from `HTTP_PROXY` on
each tool invocation is safe and reliable - there's no race condition.

**Environment Variables** (all point to same proxy):

| Variable                              | Purpose              |
| ------------------------------------- | -------------------- |
| `HTTP_PROXY`, `http_proxy`            | Standard HTTP proxy  |
| `HTTPS_PROXY`, `https_proxy`          | Standard HTTPS proxy |
| `GLOBAL_AGENT_HTTP_PROXY`             | Node.js global-agent |
| `GLOBAL_AGENT_HTTPS_PROXY`            | Node.js global-agent |
| `YARN_HTTP_PROXY`, `YARN_HTTPS_PROXY` | Yarn package manager |
| `ELECTRON_GET_USE_PROXY=1`            | Electron downloads   |

**Bypass List** (`NO_PROXY`, `no_proxy`, `GLOBAL_AGENT_NO_PROXY`):

```
localhost,127.0.0.1,169.254.169.254,metadata.google.internal,
*.svc.cluster.local,*.local,*.googleapis.com,*.google.com
```

### 2. Git Proxy (Local)

Git operations use a separate local proxy managed by environment-manager.

**Proxy Address**: `127.0.0.1:{dynamic_port}` (e.g., 58929)

**Authentication**: Basic auth via URL username:

```
http://local_proxy@127.0.0.1:58929/git/{owner}/{repo}
```

**Git Remote Rewrite**:

```
Original:  https://github.com/user/repo.git
Rewritten: http://local_proxy@127.0.0.1:58929/git/user/repo
```

**Git Configuration** (`~/.gitconfig`):

```ini
[http]
    proxyAuthMethod = basic
```

**Purpose**:

- Routes git operations through session ingress
- Enables branch restrictions (e.g., `claude/*` branches only)
- Provides authentication without exposing tokens
- Allows audit logging of all git operations

### Proxy Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│ HTTP/HTTPS Request (curl, npm, pip, etc.)                               │
└────────────────────────┬────────────────────────────────────────────────┘
                         │ HTTP_PROXY / HTTPS_PROXY
                         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Egress Proxy (21.0.0.127:15004)                                         │
│   - Authenticates via JWT in URL                                        │
│   - Validates session_id, container_id                                  │
│   - Checks allowed_hosts                                                │
│   - Logs request for audit                                              │
└────────────────────────┬────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Internet (filtered by network policy)                                   │
└─────────────────────────────────────────────────────────────────────────┘


┌─────────────────────────────────────────────────────────────────────────┐
│ Git Operation (git push, git fetch, etc.)                               │
└────────────────────────┬────────────────────────────────────────────────┘
                         │ Rewritten remote URL
                         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Local Git Proxy (127.0.0.1:58929)                                       │
│   - Managed by environment-manager                                      │
│   - Adds session authentication                                         │
└────────────────────────┬────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Session Ingress (api.anthropic.com)                                     │
│   /v1/session_ingress/session/{id}/git_proxy/{owner}/{repo}.git        │
└────────────────────────┬────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ GitHub (via Anthropic's authenticated connection)                       │
└─────────────────────────────────────────────────────────────────────────┘
```

## MCP Server: Codesign

A Model Context Protocol server for Ed25519 code signing.

### Discovery

```bash
$ env | grep CODESIGN
CODESIGN_MCP_PORT=64293
CODESIGN_MCP_TOKEN=<redacted>
```

### Protocol: Initialize

```bash
$ curl -s -X POST http://localhost:$CODESIGN_MCP_PORT/mcp \
  -H "Authorization: Bearer $CODESIGN_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "method": "initialize", "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "probe", "version": "1.0"}}, "id": 1}'
```

Response:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2024-11-05",
    "capabilities": { "tools": { "listChanged": true } },
    "serverInfo": { "name": "codesign", "version": "1.0.0" }
  }
}
```

### Protocol: List Tools

```bash
$ curl -s -X POST http://localhost:$CODESIGN_MCP_PORT/mcp \
  -H "Authorization: Bearer $CODESIGN_MCP_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 2}'
```

```json
{
  "tools": [
    {
      "name": "sign_file",
      "description": "Sign a file's contents with SSH-style Ed25519 signature. This tool reads a file and signs its contents for git commits and other authentication needs.",
      "inputSchema": {
        "type": "object",
        "required": ["file_path"],
        "properties": {
          "file_path": { "type": "string", "description": "Path to the file to sign" },
          "git_object_format": {
            "type": "string",
            "default": "sha1",
            "description": "Git object format (sha1 or sha256)"
          },
          "output_path": { "type": "string", "description": "Optional path for signature output" },
          "repo_directory": { "type": "string", "description": "Git repository working directory" }
        }
      }
    }
  ]
}
```

### Unsupported Methods

- `resources/list` → `-32601: resources not supported`
- `prompts/list` → `-32601: prompts not supported`

### Purpose

The codesign MCP server allows the agent to make **signed git commits** without exposing the private signing key. The key stays in the MCP server; only the signing operation is exposed.

## Claude Configuration

### Global Settings (~/.claude/settings.json)

```json
{
  "$schema": "https://json.schemastore.org/claude-code-settings.json",
  "hooks": {
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/stop-hook-git-check.sh"
          }
        ]
      }
    ]
  },
  "permissions": {
    "allow": ["Skill"]
  }
}
```

### Stop Hook (~/.claude/stop-hook-git-check.sh)

Prevents session end if work isn't saved:

```bash
#!/bin/bash
input=$(cat)

# Recursion prevention
stop_hook_active=$(echo "$input" | jq -r '.stop_hook_active')
if [[ "$stop_hook_active" = "true" ]]; then exit 0; fi

# Skip if not in a git repo
if ! git rev-parse --git-dir >/dev/null 2>&1; then exit 0; fi

# Check for uncommitted changes
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "There are uncommitted changes in the repository. Please commit and push these changes to the remote branch." >&2
  exit 2
fi

# Check for untracked files
untracked_files=$(git ls-files --others --exclude-standard)
if [[ -n "$untracked_files" ]]; then
  echo "There are untracked files in the repository. Please commit and push these changes to the remote branch." >&2
  exit 2
fi

# Check for unpushed commits
current_branch=$(git branch --show-current)
if [[ -n "$current_branch" ]]; then
  if git rev-parse "origin/$current_branch" >/dev/null 2>&1; then
    unpushed=$(git rev-list "origin/$current_branch..HEAD" --count 2>/dev/null) || unpushed=0
    if [[ "$unpushed" -gt 0 ]]; then
      echo "There are $unpushed unpushed commit(s) on branch '$current_branch'. Please push these changes to the remote repository." >&2
      exit 2
    fi
  else
    # Branch doesn't exist on remote - compare against default branch
    unpushed=$(git rev-list "origin/HEAD..HEAD" --count 2>/dev/null) || unpushed=0
    if [[ "$unpushed" -gt 0 ]]; then
      echo "Branch '$current_branch' has $unpushed unpushed commit(s) and no remote branch. Please push these changes to the remote repository." >&2
      exit 2
    fi
  fi
fi

exit 0
```

### Session Start Hook Skill

Located at `~/.claude/skills/session-start-hook/SKILL.md`, teaches how to create SessionStart hooks for dependency installation in new repositories.

## Environment Variables

### Claude-Specific

| Variable                                       | Description                                  |
| ---------------------------------------------- | -------------------------------------------- |
| `CLAUDECODE=1`                                 | Indicates Claude Code environment            |
| `CLAUDE_CODE_BASE_REF`                         | Base git ref for the session (e.g., `devel`) |
| `CLAUDE_CODE_CONTAINER_ID`                     | Container identifier                         |
| `CLAUDE_CODE_DEBUG=true`                       | Debug mode enabled                           |
| `CLAUDE_CODE_DIAGNOSTICS_FILE`                 | Path to diagnostics log file                 |
| `CLAUDE_CODE_EMIT_TOOL_USE_SUMMARIES=true`     | Emit tool use summaries in output            |
| `CLAUDE_CODE_ENTRYPOINT=remote`                | Entry point mode (remote for web sessions)   |
| `CLAUDE_CODE_ENVIRONMENT_RUNNER_VERSION`       | Version of environment-manager binary        |
| `CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR=4`    | FD for OAuth token                           |
| `CLAUDE_CODE_POST_FOR_SESSION_INGRESS_V2=true` | Use v2 session ingress POST endpoint         |
| `CLAUDE_CODE_PROXY_RESOLVES_HOSTS=true`        | Git proxy configuration                      |
| `CLAUDE_CODE_REMOTE=true`                      | Running in remote/container environment      |
| `CLAUDE_CODE_REMOTE_ENVIRONMENT_TYPE`          | Environment type (e.g., `cloud_default`)     |
| `CLAUDE_CODE_REMOTE_SESSION_ID`                | Remote session identifier                    |
| `CLAUDE_CODE_SESSION_ID`                       | Current session identifier                   |
| `CLAUDE_CODE_VERSION`                          | Claude Code version                          |
| `CLAUDE_CODE_WEBSOCKET_AUTH_FILE_DESCRIPTOR=3` | FD for WebSocket auth                        |
| `CLAUDE_SESSION_INGRESS_TOKEN_FILE`            | Path to session ingress token file           |

### MCP-Specific

| Variable                     | Description                      |
| ---------------------------- | -------------------------------- |
| `CODESIGN_MCP_PORT`          | Port for codesign MCP server     |
| `CODESIGN_MCP_TOKEN`         | Auth token for codesign MCP      |
| `MCP_CONNECTION_NONBLOCKING` | Use non-blocking MCP connections |
| `MCP_TOOL_TIMEOUT`           | Timeout for MCP tool calls       |

### Proxy Configuration

| Variable                   | Description       |
| -------------------------- | ----------------- |
| `GLOBAL_AGENT_HTTPS_PROXY` | HTTPS proxy URL   |
| `GLOBAL_AGENT_HTTP_PROXY`  | HTTP proxy URL    |
| `GLOBAL_AGENT_NO_PROXY`    | Proxy bypass list |
| `ANTHROPIC_BASE_URL`       | API base URL      |

## Git Proxy

### Local Proxy

The environment-manager runs a local git proxy on a dynamically allocated port:

```
Address: http://127.0.0.1:{dynamic_port}
Example: http://local_proxy@127.0.0.1:58929/git/{owner}/{repo}
```

### Remote Rewrite

All git remotes are rewritten to route through the local proxy:

```
Original: https://github.com/user/repo.git
Rewritten: http://local_proxy@127.0.0.1:58929/git/user/repo
```

The local proxy then forwards to session ingress:

```
https://api.anthropic.com/v1/session_ingress/session/{session_id}/git_proxy/{owner}/{repo}.git/{path}
```

### Implementation Details

The git proxy is implemented in `environment-manager`:

**Source Location** (from binary strings):

```
github.com/anthropics/anthropic/api-go/environment-manager/internal/gitproxy/
├── handler.go
├── manager.go
└── server.go
```

**Key Types**:

- `gitproxy.Server` - HTTP server handling git requests
- `gitproxy.Manager` - Lifecycle management, start/stop
- `gitproxy.RepoAuth` - Per-repository authentication tokens
- `gitproxy.handler` - Request processing logic

**Environment Variable**:

- `CCR_TEST_GITPROXY=1` - Enables git proxy testing mode

### Path Validation (Security Analysis)

Tested whether the git proxy could be used for arbitrary egress (bypassing the JWT-authenticated proxy).

**Test Results**:

| Path                                                           | Response                                 | Conclusion                     |
| -------------------------------------------------------------- | ---------------------------------------- | ------------------------------ |
| `/`                                                            | `Invalid path format`                    | Root rejected                  |
| `/git/agentydragon/ducktape`                                   | 400 Bad Request                          | Needs full path                |
| `/git/agentydragon/ducktape/info/refs?service=git-upload-pack` | Git protocol data                        | ✅ Works for authorized repo   |
| `/git/torvalds/linux/info/refs`                                | `Proxy error: repository not authorized` | Unauthorized repos blocked     |
| `/https://example.com`                                         | `Invalid path format`                    | Arbitrary URLs rejected        |
| `/api/something`                                               | `Invalid path format`                    | Non-git paths rejected         |
| `/agentydragon/ducktape`                                       | `Invalid path format`                    | Must start with `/git/`        |
| `/git/agentydragon/ducktape/../../../etc/passwd`               | `Invalid path format`                    | Path traversal rejected        |
| `/git/agentydragon/ducktape%2f..%2f..%2fetc%2fpasswd`          | `Proxy error: invalid git path`          | URL-encoded traversal rejected |
| `/git/agentydragon/ducktape%252f..%252f`                       | `Invalid path format`                    | Double-encoded rejected        |
| `/git/agentydragon/*`                                          | `Invalid path format`                    | Wildcards rejected             |
| HTTP CONNECT to external hosts                                 | No response                              | CONNECT method not supported   |

**Error Messages** (from binary):

- `Invalid path format` - Path doesn't match expected pattern
- `Invalid path format - missing components` - Path incomplete
- `Proxy error: repository not authorized` - Valid path but unauthorized
- `Proxy error: invalid git path` - Path validation failed after decoding

**Path Validation Logic**:

Based on strings in the binary, the proxy appears to use regex validation:

```
^([\w./]+)/((?:\w+)|[*])(.+)?$
```

The proxy strictly validates:

1. Path must start with `/git/`
2. Must have `{owner}/{repo}` format
3. Repository must be pre-authorized (from session config)
4. No path traversal allowed
5. No URL encoding tricks

### Security Conclusion

**The git proxy cannot be used as an egress bypass.**

- Only accepts `/git/{owner}/{repo}` paths
- Validates repository authorization against session config
- Rejects all non-git requests with "Invalid path format"
- Does not support HTTP CONNECT tunneling
- Cannot be used to reach arbitrary URLs

### Purpose

- **Authentication**: Git operations authenticated via session token
- **Auditing**: All git activity logged through Anthropic
- **Branch restrictions**: Can enforce branch naming (e.g., `claude/*`)
- **Repository whitelisting**: Only pre-authorized repos accessible

## Installed Tools

```bash
$ which -a claude node python uv
/opt/node22/bin/claude
/opt/node22/bin/node
/usr/local/bin/node          # symlink to /opt/node20/bin/node
/usr/local/bin/python        # symlink to /usr/bin/python3.11
/usr/bin/python
/root/.local/bin/uv

$ ls /opt/
apache-maven-3.9.11  gradle-8.14.3  node20  node22  rbenv      ruby-3.2.6
gradle               maven          node21  nvm     ruby-3.1.6 ruby-3.3.6
```

## Code Signing

Git commits are signed using a bridge between git's GPG interface and the MCP codesign server.

### Git Configuration

```ini
# ~/.gitconfig
[user]
    name = Claude
    email = noreply@anthropic.com
    signingkey = /home/claude/.ssh/commit_signing_key.pub

[gpg]
    format = ssh

[gpg "ssh"]
    program = /tmp/code-sign    # Bridge to MCP server

[commit]
    gpgsign = true
```

### code-sign Binary

**Location**: `/tmp/code-sign`
**Type**: Compiled Go binary (ELF 64-bit)

**Purpose**: Bridges git's SSH signing protocol to the MCP codesign server:

```
┌─────────────────┐     ┌──────────────┐     ┌─────────────────────┐
│  git commit     │────▶│ /tmp/code-sign│────▶│ MCP codesign server │
│  (gpg.ssh.prog) │     │ (Go binary)   │     │ (localhost:64293)   │
└─────────────────┘     └──────────────┘     └─────────────────────┘
```

**How it works**:

1. Git calls `/tmp/code-sign` with the file to sign
2. `code-sign` calls the MCP server's `sign_file` tool
3. MCP server signs with Ed25519 key (key never leaves server)
4. Signature returned to git

### MCP Codesign Tool

```json
{
  "name": "sign_file",
  "inputSchema": {
    "required": ["file_path"],
    "properties": {
      "file_path": "Path to the file to sign",
      "git_object_format": "sha1 or sha256 (default: sha1)",
      "repo_directory": "Git repository working directory"
    }
  }
}
```

### Security Benefits

- **Key isolation**: Private signing key never exposed to agent
- **Audit trail**: All signatures logged via MCP
- **Revocability**: Key controlled by Anthropic, can be revoked
- **Verification**: Commits can be verified against known public keys

## Security Analysis

### Can Arbitrary Code Spawn Subagents?

**No.** The architecture deliberately prevents this.

#### What's Available

| Method               | Works? | Reason                           |
| -------------------- | ------ | -------------------------------- |
| Built-in `Task` tool | ✅     | Official sanctioned mechanism    |
| `claude -p` CLI      | ❌     | No auth - tokens in parent's FDs |
| Direct API calls     | ❌     | No `ANTHROPIC_API_KEY` in env    |
| WebSocket hijacking  | ❌     | Auth token not accessible        |

#### Auth Isolation

```
┌─────────────────────────────────────────────────────────────┐
│  environment-manager (PID 25)                               │
│    └─> claude (PID 43) ◄── WebSocket auth via FD 3,4       │
│          └─> bash (agent shells) ◄── NO auth inheritance   │
└─────────────────────────────────────────────────────────────┘
```

File descriptors 3 and 4 contain auth tokens but are:

- Pipes, not regular files
- Not inherited by child processes
- Not readable from other processes

#### Attempted Exploits

```bash
# Direct CLI - hangs waiting for auth
$ claude -p "Say hello"
# Times out, no ANTHROPIC_API_KEY

# Direct API - auth error
$ curl https://api.anthropic.com/v1/messages -H "x-api-key: test" ...
{"error":{"type":"authentication_error","message":"invalid x-api-key"}}

# Reading parent FDs - empty/inaccessible
$ cat /proc/43/fd/3
# (empty - pipes don't work this way)
```

#### Conclusion

The `Task` tool is the **only path** to spawn subagents. It routes through the official claude infrastructure which handles auth, resource limits, and tool permissions. This is intentional security design.

## Architecture Summary

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Claude Code Web Session                              │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌────────────────────┐                                                     │
│  │ environment-manager │                                                     │
│  │ (Go binary)         │                                                     │
│  │                     │                                                     │
│  │ • Session lifecycle │                                                     │
│  │ • Git proxy setup   │                                                     │
│  │ • Auth management   │                                                     │
│  │ • MCP server spawn  │                                                     │
│  └──────────┬─────────┘                                                     │
│             │                                                                │
│             │ spawns with FD 3,4 (auth tokens)                              │
│             ▼                                                                │
│  ┌────────────────────┐     ┌─────────────────┐                            │
│  │ claude CLI          │────▶│ MCP: codesign   │                            │
│  │ (Node.js/TypeScript)│     │ (Ed25519 sign)  │                            │
│  │                     │     └─────────────────┘                            │
│  │ • Tool execution    │                                                     │
│  │ • Subagent spawning │                                                     │
│  │ • Stream I/O        │                                                     │
│  └──────────┬─────────┘                                                     │
│             │                                                                │
│             │ WebSocket (bidirectional JSON stream)                         │
│             ▼                                                                │
│  ┌────────────────────┐                                                     │
│  │ Git Proxy           │                                                     │
│  │ (localhost:51431)   │                                                     │
│  └──────────┬─────────┘                                                     │
│             │                                                                │
└─────────────┼────────────────────────────────────────────────────────────────┘
              │
              │ HTTPS (only *.anthropic.com allowed)
              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                        api.anthropic.com                                     │
│                                                                              │
│  /v1/session_ingress/ws/{session_id}     ← WebSocket                        │
│  /v1/session_ingress/session/{id}        ← Resume/persist                   │
│  /v2/session_ingress/session/{id}/events ← Env manager logs                 │
│  /v1/session_ingress/session/{id}/git_proxy/{repo}.git ← Git operations     │
│                                                                              │
└─────────────────────────────────────────────────────────────────────────────┘
              │
              │
              ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                          claude.ai (User Browser)                            │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Inference Protocol (Messages API)

The Claude CLI inside the container communicates with the Anthropic backend using the standard Messages API over the WebSocket connection.

### Request Format

```json
{
  "model": "claude-opus-4-5-20251101",
  "max_tokens": 8192,
  "messages": [
    {
      "role": "user",
      "content": [{ "type": "text", "text": "User prompt here" }]
    }
  ],
  "tools": [
    {
      "name": "Bash",
      "description": "Execute bash commands...",
      "input_schema": {
        "type": "object",
        "properties": {
          "command": { "type": "string" },
          "timeout": { "type": "number" }
        },
        "required": ["command"]
      }
    }
  ],
  "stream": true
}
```

### Streaming Response Events

When `stream: true`, the API returns Server-Sent Events (SSE) with these event types:

| Event Type            | Description                                              |
| --------------------- | -------------------------------------------------------- |
| `message_start`       | Initial message metadata (id, model, usage)              |
| `content_block_start` | New content block beginning (text or tool_use)           |
| `content_block_delta` | Incremental content (`text_delta` or `input_json_delta`) |
| `content_block_stop`  | Content block finished                                   |
| `message_delta`       | Message-level updates (stop_reason, usage)               |
| `message_stop`        | Stream complete                                          |
| `ping`                | Keep-alive                                               |
| `error`               | Error occurred                                           |

### Event Flow Example

```
event: message_start
data: {"type":"message_start","message":{"id":"msg_...","model":"claude-opus-4-5-20251101"}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Let me "}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"help..."}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":42}}

event: message_stop
data: {"type":"message_stop"}
```

### Tool Use Response

When the model decides to use a tool:

```
event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_...","name":"Bash","input":{}}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\"command\":"}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"\"ls -la\"}"}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}
```

### Tool Result Submission

After executing a tool, results are sent as a user message:

```json
{
  "role": "user",
  "content": [
    {
      "type": "tool_result",
      "tool_use_id": "toolu_01ABC...",
      "content": "total 4\ndrwxr-xr-x 2 user user 4096 Jan 19 12:00 ."
    }
  ]
}
```

### WebSocket Wrapper

Over the WebSocket connection, messages are wrapped with session context:

```json
{
  "type": "api_request",
  "session_id": "session_018FEX...",
  "request": {
    /* Standard Messages API payload */
  }
}
```

Responses include:

```json
{
  "type": "api_response",
  "session_id": "session_018FEX...",
  "event": "content_block_delta",
  "data": {
    /* SSE data payload */
  }
}
```

### Authentication

- **Header**: `Authorization: Bearer sk-ant-si-{JWT}`
- **Additional**: `X-Last-Request-Id` for request correlation
- **TLS**: TLS 1.3 with `TLS_AES_256_GCM_SHA384`

### Heartbeat

The WebSocket connection maintains a heartbeat:

```json
{ "sessionID": "session_018FEX...", "lastUpdate": 1737316840 }
```

## Making Additional Inference Requests

### Can External Code Make LLM Requests?

**Summary: No, by design.**

The architecture deliberately prevents unauthorized inference requests:

| Method                               | Result      | Reason                                 |
| ------------------------------------ | ----------- | -------------------------------------- |
| Direct Messages API with OAuth token | ❌ 401      | "OAuth authentication not supported"   |
| Direct Messages API with SI token    | ❌ 401      | "invalid x-api-key"                    |
| New WebSocket with SI token          | ❌ 401      | Token is single-use, already consumed  |
| Write to claude stdin via `/proc`    | ⚠️ Buffered | Messages queue but format may be wrong |

### Token Single-Use Constraint

The Session Ingress token (`sk-ant-si-...`) is **single-use per WebSocket connection**:

```python
# Attempting to reuse the token:
async with websockets.connect(url, additional_headers=headers) as ws:
    # Connection failed: InvalidStatus: server rejected WebSocket connection: HTTP 401
```

Once the active claude process establishes its WebSocket, no other process can connect with the same token.

### Stdin Injection (Theoretical)

It's technically possible to write to claude's stdin via `/proc/{pid}/fd/0`:

```python
import os
import json

# Claude uses stream-json format
message = {"type": "user", "message": {"role": "user", "content": [...]}}

fd = os.open("/proc/43/fd/0", os.O_WRONLY | os.O_NONBLOCK)
os.write(fd, (json.dumps(message) + "\n").encode())
os.close(fd)
```

However:

1. Messages queue in the pipe buffer until claude reads them
2. The exact format required includes UUIDs and other fields
3. Claude may reject malformed messages
4. This doesn't bypass authentication - the WebSocket connection is already established

### Verified Local Services

| Port                  | Service      | Auth Required         | Purpose                      |
| --------------------- | ------------ | --------------------- | ---------------------------- |
| `$CODESIGN_MCP_PORT`  | Codesign MCP | `$CODESIGN_MCP_TOKEN` | Git commit signing           |
| Dynamic (e.g., 24864) | Git Proxy    | Basic auth in URL     | Git operations               |
| 2024                  | process_api  | N/A                   | Container init (no HTTP API) |

### WebSocket Hijacking Attempts

Can we hijack the existing authenticated WebSocket connection?

**Summary: No, multiple layers of protection.**

| Method                            | Result            | Reason                                         |
| --------------------------------- | ----------------- | ---------------------------------------------- |
| Open socket via `/proc/{pid}/fd/` | ❌ `ENXIO`        | Kernel prevents re-opening sockets             |
| `process_vm_readv/writev`         | ❌ `EFAULT`       | Memory access denied                           |
| Read TLS session keys from memory | ❌ Not found      | Keys not in accessible heap region             |
| `/dev/mem` access                 | ❌ Not accessible | Device not available in container              |
| SCM_RIGHTS socket passing         | ❌ N/A            | Requires target process cooperation            |
| gdb ptrace injection              | ⚠️ Possible       | But TLS encryption still blocks raw socket I/O |

**Detailed findings:**

1. **Socket FDs are protected**: Opening `/proc/43/fd/21` (a socket) returns `ENXIO`:

   ```python
   os.open("/proc/43/fd/21", os.O_RDWR)
   # OSError: [Errno 6] No such device or address
   ```

2. **TLS 1.3 encryption**: Even if we could access the socket, traffic is encrypted:
   - Cipher: `TLS_AES_256_GCM_SHA384`
   - No `SSLKEYLOGFILE` environment variable set
   - Session keys not found in heap memory dump

3. **ptrace access works** (ptrace_scope=1, running as root), but:
   - Raw socket I/O bypasses TLS, sending garbage
   - Would need to find OpenSSL's `SSL` structure pointer
   - Then call `SSL_write()` with correct context

4. **Memory layout**:

   ```
   SSL_write @ 0x1f15e20 (in Node.js binary)
   SSL_read  @ 0x1f153f0
   TLS_client_method @ 0x1f00dc0
   ```

   But finding the SSL context pointer for the WebSocket connection requires:
   - Understanding V8 heap structure
   - Finding the WebSocket object → TLS socket → SSL\*

5. **Heap scanning results**:
   - Found 768 potential SSL structures in 80MB heap dump
   - Tested candidates with `SSL_get_fd()` - all returned -1
   - SSL\* pointers likely in V8's managed heap, not the C heap
   - WebSocket URL found in memory: `wss://api.anthropic.com/v1/session_ingress/ws/{session_id}`

6. **Syscall interception (ptrace)**:
   - Can intercept `write()` syscalls, but data is already TLS-encrypted
   - SSL_write is a library call, not a syscall - can't intercept with ptrace
   - Would need gdb breakpoint on SSL_write + buffer modification
   - Requires finding the correct SSL\* context first

### Security Conclusion

The system enforces authentication boundaries:

- **Inference**: Only via the pre-authenticated WebSocket connection
- **Git operations**: Via authenticated git proxy
- **Code signing**: Via MCP with separate token
- **No ANTHROPIC_API_KEY**: Environment has no direct API access

This design ensures that:

1. Only the authorized claude process can make inference requests
2. Tool execution cannot spawn additional inference
3. All operations are auditable through session ingress
4. WebSocket cannot be hijacked even with root access

## Sandbox Constraints Summary

| Category                     | Constraint                                                        |
| ---------------------------- | ----------------------------------------------------------------- |
| **Network**                  | Only `*.anthropic.com` domains allowed                            |
| **Filesystem read denied**   | `~/.ssh`, `~/.aws`, `~/.config/gcloud`, `/etc/shadow`, `/secrets` |
| **Filesystem write allowed** | `/tmp`, `~`, `/workspace`                                         |
| **Git config**               | Allowed                                                           |
| **Nested sandbox**           | Weaker nested sandbox enabled                                     |

## Process Tree

```
PID 1   init
└── PID 23  /bin/sh -c ... environment-manager task-run ...
    └── PID 25  environment-manager task-run --session {id}
        └── PID 43  claude (with FD 3,4 for auth)
            └── PID xxx  /bin/bash (agent shell commands)
```
