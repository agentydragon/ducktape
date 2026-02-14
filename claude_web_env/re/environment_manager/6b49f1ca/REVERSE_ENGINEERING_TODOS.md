# Environment Manager Reverse Engineering - TODO

This document tracks incomplete reverse engineering work and stubs that need implementation.

## ✅ Recently Completed

- **MCP Server Registration** (`internal/mcp/server.go`) - Tool registration, HTTP serving, heartbeat loop
- **CodeSign Server** (`internal/mcp/servers/codesign/server.go`) - GetTools() returns proper schema
- **Manager MCP Registration** (`internal/manager/manager.go`) - Logs registration results/errors
- **Tunnel Client** (`internal/tunnel/factory.go`) - Factory implementation, circular dependency resolved
- **Vercel CollectFiles** (`internal/tunnel/actions/deploy/vercel.go`) - Directory walking, SHA1 hashing, 100MB limit
- **Git Proxy URL** (`internal/gitproxy/handler.go`) - SessionID added to URL path
- **WebSocket URL Conversion** (`internal/manager/manager.go:createTunnelClient`) - http→ws, https→wss conversion (0xb6db91-0xdbfe)
- **Tunnel Client Creation** (`internal/manager/manager.go:createTunnelClient`) - Full implementation with action registry, deploy action, and conditional tunnel setup based on environment sub type "baku" (0xb6dae0-0xb6e028)

## 🔴 Critical Stubs (Affects Functionality)

### None Currently

All critical functionality stubs have been implemented. Remaining work is configuration wiring and observability.

## 🟢 Observability & Metrics (Non-Critical) - COMPLETE ✅

### Command Timing - COMPLETE ✅

- ✅ **All duration metrics wired** - stdin parse, claude code install, total parse, total setup, manager run, healthcheck

### Git Operations - COMPLETE ✅

- ✅ **Timing metrics added** (`internal/sources/git.go`) - git validation and fetch duration recorded on success/failure
- Note: `activityMsg` variables are logged but not sent to activityRecorder (low priority)

### Manager Metrics - COMPLETE ✅

- ✅ **Manager run duration** (`internal/manager/manager.go`) - elapsed time now recorded

## 🟡 Configuration Wiring (Medium Priority)

### Command Flags - Task Run (`cmd/cmd_task_run.go`)

- ✅ ~~`skipGitConfig`~~ - **FIXED** (already working via direct os.Getenv in setupGitConfig/configureGitSigning)
- ✅ ~~`activityRecorder`~~ - **FIXED** (wired through stdinConfigClient with proper HttpSessionIngressClient, NoopActivityRecorder fallback)
- ✅ ~~`otelEndpoint`~~ - **FIXED** (wired to O11yConfig initialization)
- 🟡 `sessionMode` - **PARTIAL** (field added to Manager, passed from CLI, logged in configureEnvironment; needs SetSessionMode call on environment type)
- `inputFormatChanged` - needs conditional logic implementation (complex binary behavior at 0xb78d20-0xb78ddb)

### API & Service Setup

- ✅ ~~`activityRecorder`~~ - **FIXED** (see above)
- ✅ ~~`otelEndpoint`~~ - **FIXED** (see above)
- `o11yService` - returned from init but not used (may be intentional)

### Poll Command (`cmd/cmd_poll.go:102, 160, 161`)

- `identity`, `maxPollRetries`, `secretKeyEnv` not wired

### Orchestrator (`cmd/cmd_orchestrator.go:128`)

- `PollHook` created but not passed to orchestrator

## Type Recovery (Low Impact)

### Manager Config (`internal/manager/manager.go:37`)

- `Config interface{}` should be concrete environment config client

### Claude Executor (`internal/claude/claude_code_executor.go:50`)

- Multiple fields typed as `interface{}` instead of concrete types

### Session Ingress Logs - COMPLETE ✅

- ✅ **Fixed log submission** (`internal/api/session_ingress_client.go`) - `logs` param now typed as `[]DiagLogEntry`, calls `diagLogsToWireFormat()`, uses `len(logs)` for metrics

## Incomplete Reconstructions

### Claude Executor Environment Setup (`internal/claude/claude_code_executor.go:278, 336, 337, 379, 429`)

- Environment variable setup, pipe cleanup closures, config access patterns incomplete

### Session Ingress OTel (`internal/api/session_ingress_client.go:114`)

- OTel propagator created but injection call not reconstructed

### Protobuf Types (`internal/tunnel/tunnelpb/`)

- ⚠️ Note: Now using proper protoc-generated code instead of manual reconstructions

### DataDog Metrics (`internal/dogmetrics/dogmetrics.go`)

- Entire package stubbed - `IncrementCounter()` does nothing

## Priority Order

### Critical (Breaks Core Features) - ALL COMPLETED ✅

1. ✅ ~~Tunnel client creation~~ - **FIXED** (implemented in `tunnel/factory.go`)
2. ✅ ~~Vercel file collection~~ - **FIXED** (`vercel.go:122-146` - CollectFiles fully implemented)
3. ✅ ~~WebSocket URL conversion and tunnel client setup~~ - **FIXED** (`manager.go:createTunnelClient` - http→ws/https→wss conversion, action registry creation, conditional tunnel setup)

### Medium (Degrades Features)

1. Command flag wiring
2. Activity recorder and telemetry plumbing
3. Session ingress log submission
4. Claude executor environment setup
5. ✅ ~~Git proxy URL construction~~ - **FIXED** (sessionID added to URL)
6. Streamable server cleanup (partial)

### Low (Missing Telemetry/Type Safety)

1. Duration metrics and o11y timing
2. Type recovery (interface{} → concrete types)
3. DataDog metrics
4. OTel context propagation
