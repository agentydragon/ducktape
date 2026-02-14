# Environment Manager Reverse Engineering - TODO

This document tracks incomplete reverse engineering work and stubs that need implementation.

## ✅ Recently Completed

- **MCP Server Registration** (`internal/mcp/server.go`) - Tool registration, HTTP serving, heartbeat loop
- **CodeSign Server** (`internal/mcp/servers/codesign/server.go`) - GetTools() returns proper schema
- **Manager MCP Registration** (`internal/manager/manager.go`) - Logs registration results/errors

## 🔴 Critical Stubs (Affects Functionality)

### Tunnel Registration (`internal/manager/tunnel_register.go:64`)

- `defaultNewTunnelClient()` returns `nil` - should forward params to `tunnel.NewClient`
- **Impact**: Tunnel functionality completely broken

### Manager Functions (`internal/manager/manager.go:261-263, 432-434`)

- `handleActivity()` - should dispatch to environment type's activity handler
- `constructWebSocketURL()` - should convert http→ws / https→wss
- **Impact**: Activity reporting and WebSocket connections won't work

### Vercel Deploy (`internal/tunnel/actions/deploy/vercel.go:122-146`)

- `CollectFiles()` function body empty - should walk directory, read files, compute SHA256 hashes
- **Impact**: Vercel deployment won't work

## 🟢 Observability & Metrics (Non-Critical)

### Command Timing (`cmd/cmd_task_run.go:198, 202, 225, 252, 292, 323, 325, 340`)

- Unused duration variables should be logged as o11y metrics

### Git Operations (`internal/sources/git.go:313, 398, 717`)

- `activityMsg` should be sent to activityRecorder
- `startTime`/`elapsed` should be used for metrics

### Manager Metrics (`internal/manager/manager.go:79`)

- `startTime` not used for elapsed time calculation

## 🟡 Configuration Wiring (Medium Priority)

### Command Flags (`cmd/cmd_task_run.go:105, 140, 144, 206`)

- `inputFormatChanged`, `sessionMode`, `skipGitConfig` not wired to behavior

### API & Service Setup (`cmd/cmd_task_run.go:202, 252, 259`)

- `activityRecorder`, `otelEndpoint`, `o11yService` not wired to manager/diagnostics

### Poll Command (`cmd/cmd_poll.go:102, 160, 161`)

- `identity`, `maxPollRetries`, `secretKeyEnv` not wired

### Orchestrator (`cmd/cmd_orchestrator.go:128`)

- `PollHook` created but not passed to orchestrator

## Type Recovery (Low Impact)

### Manager Config (`internal/manager/manager.go:37`)

- `Config interface{}` should be concrete environment config client

### Claude Executor (`internal/claude/claude_code_executor.go:50`)

- Multiple fields typed as `interface{}` instead of concrete types

### Session Ingress Logs (`internal/api/session_ingress_client.go:210, 219, 229, 240`)

- `logs` param typed as `interface{}`, hardcoded `num_logs: 0`

## Incomplete Reconstructions

### Claude Executor Environment Setup (`internal/claude/claude_code_executor.go:278, 336, 337, 379, 429`)

- Environment variable setup, pipe cleanup closures, config access patterns incomplete

### Session Ingress OTel (`internal/api/session_ingress_client.go:114`)

- OTel propagator created but injection call not reconstructed

### Git Proxy Handler (`internal/gitproxy/handler.go:237`)

- `upstreamURL` param accepted but never used

### Protobuf Types (`internal/tunnel/tunnelpb/`)

- ⚠️ Note: Now using proper protoc-generated code instead of manual reconstructions

### DataDog Metrics (`internal/dogmetrics/dogmetrics.go`)

- Entire package stubbed - `IncrementCounter()` does nothing

## Priority Order

### Critical (Breaks Core Features)

1. Tunnel client creation (`tunnel_register.go:64`)
2. Vercel file collection (`vercel.go:122-146`)
3. Manager activity handling and WebSocket URL (`manager.go:261-263, 432-434`)

### Medium (Degrades Features)

1. Command flag wiring
2. Activity recorder and telemetry plumbing
3. Session ingress log submission
4. Claude executor environment setup
5. Git proxy URL construction
6. Streamable server cleanup (partial)

### Low (Missing Telemetry/Type Safety)

1. Duration metrics and o11y timing
2. Type recovery (interface{} → concrete types)
3. DataDog metrics
4. OTel context propagation
