# Environment Manager Reverse Engineering - Incomplete Items

This document tracks areas where the reverse engineering is incomplete, stubbed, or where the reconstructed code doesn't fully capture the binary's behavior.

## Critical Stubs (Affects Functionality)

### MCP Server Registration (`internal/mcp/server.go`)

**Lines 142, 193, 198, 238**

- `GetTools()` returns `nil, 0, 0` - should call `s.GetTools()` and register tools with MCP server
- HTTP server serving is stubbed - should call `s.httpServer.Serve(listener)`
- Heartbeat loop body not reconstructed (~1.5KB at 0xad0d00)
- Streamable server cleanup not reconstructed
- **Impact**: MCP servers won't actually register their tools or serve requests

### CodeSign Server (`internal/mcp/servers/codesign/server.go`)

**Lines 162-189**

- `GetTools()` creates property maps but returns `nil, 0, 0` instead of actual tool definitions
- Should construct and return MCP tool schema for `sign_file` tool
- **Impact**: CodeSign MCP server won't expose its tool to Claude

### Tunnel Registration (`internal/manager/tunnel_register.go`)

**Line 64**

- Function is a complete stub - returns `nil` instead of creating tunnel client
- Should forward params to `tunnel.NewClient` and return result
- **Impact**: Tunnel functionality completely broken

### Manager Functions (`internal/manager/manager.go`)

**Lines 261-263, 432-434**

- `handleActivity()` stub - should dispatch to environment type's activity handler
- `constructWebSocketURL()` stub - should convert API URL from http→ws/https→wss
- **Impact**: Activity reporting and WebSocket connections won't work

### Vercel Deploy (`internal/tunnel/actions/deploy/vercel.go`)

**Lines 122-146**

- `CollectFiles()` function body mostly empty - only has comments
- Should walk directory, read files, compute SHA256 hashes, build FileEntry slice
- **Impact**: Vercel deployment action won't work - can't collect files to deploy

## Observability & Metrics (Non-Critical)

### Command Timing (`cmd/cmd_task_run.go`)

**Lines 198, 202, 225, 252, 292, 323, 325, 340**

- Multiple duration variables unused: `stdinParseDuration`, `installDuration`, `healthcheckDuration`, `totalParseTime`, `totalSetupTime`, `managerDuration`
- Should be logged or recorded as o11y metrics
- **Impact**: Missing timing telemetry

### Git Operations (`internal/sources/git.go`)

**Lines 313, 398, 717**

- `activityMsg` should be sent to activityRecorder
- `startTime` and `elapsed` should be used for o11y metrics
- **Impact**: Missing activity tracking and performance metrics

### Manager Metrics (`internal/manager/manager.go`)

**Line 79**

- `startTime` not used for elapsed time calculation
- **Impact**: Missing manager duration metrics

## Configuration Wiring (Medium Priority)

### Command Flags (`cmd/cmd_task_run.go`)

**Lines 105, 140, 144, 206**

- `inputFormatChanged` should gate input format behavior
- `sessionMode` should be passed to session/manager config
- `skipGitConfig` should be passed to environment config
- **Impact**: Some command-line flags don't affect behavior

### API & Service Setup (`cmd/cmd_task_run.go`)

**Lines 202, 252, 259**

- `activityRecorder` should be passed to manager/environment setup
- `otelEndpoint` should be passed to o11y.NewO11yService config
- `o11yService` should be wired into manager/diagnostics
- **Impact**: Incomplete telemetry integration

### Poll Command (`cmd/cmd_poll.go`)

**Lines 102, 160, 161**

- `identity` fields should update sessionID/workID
- `maxPollRetries` should be wired into poller retry config
- `secretKeyEnv` should be used as fallback secret key source
- **Impact**: Some configuration options ignored

### Orchestrator (`cmd/cmd_orchestrator.go`)

**Line 128**

- `PollHook` created but result discarded - should be passed to orchestrator
- **Impact**: Poll hooks won't be registered

## Type Recovery (Low Impact on Build)

### Manager Config (`internal/manager/manager.go`)

**Line 37**

```go
Config interface{} // TODO(re): concrete type not recovered
```

- Should be specific environment config client interface
- Currently typed as `interface{}`

### Claude Executor (`internal/claude/claude_code_executor.go`)

**Line 50**

```go
// TODO(re): many fields typed as interface{} — concrete types not yet recovered
```

- Multiple struct fields are `interface{}` instead of concrete types
- **Impact**: Loss of type safety, but builds and runs

### Session Ingress Logs (`internal/api/session_ingress_client.go`)

**Lines 210, 219, 229, 240**

- `logs` param typed as `interface{}` instead of `[]DiagLogEntry`
- Should call `diagLogsToWireFormat(logs, len(logs))`
- Hardcoded `num_logs: 0` instead of `len(logs)`
- **Impact**: Log submission may not work correctly

## Incomplete Reconstructions

### Claude Executor Environment Setup (`internal/claude/claude_code_executor.go`)

**Lines 278, 336, 337, 379, 429**

- Environment variable setup not reconstructed - should call `GetClaudeEnvironmentVariables(...)`
- Pipe write-end cleanup closure (func3 at 0xadf1c0) not reconstructed
- Output writer closure (func4 at 0xadf0a0) not reconstructed
- Config access patterns incomplete - should access as `*config.ClaudeConfig`
- **Impact**: Claude Code execution environment may be incomplete

### MCP Server Results (`internal/manager/manager.go`)

**Lines 467-468**

- `registeredServers` list not used/logged
- `errors` slice not checked/logged
- **Impact**: MCP registration failures silently ignored

### Session Ingress OTel (`internal/api/session_ingress_client.go`)

**Line 114**

- OTel propagator created but injection call not reconstructed
- **Impact**: Distributed tracing context not propagated

### Git Proxy Handler (`internal/gitproxy/handler.go`)

**Line 237**

- URL construction incomplete - `upstreamURL` param accepted but never used
- **Impact**: Git proxy may not construct correct upstream URLs

### Protobuf Types (`internal/tunnel/tunnelpb/types.go`)

**Lines 1, 5, 28-30, 72-74, etc.**

- All ProtoReflect() methods return `nil` (stubs)
- Types are manual reconstructions, not protoc-generated
- Missing full protobuf reflection support
- **Impact**: Some protobuf operations may fail, but basic marshaling works

### DataDog Metrics (`internal/dogmetrics/dogmetrics.go`)

**Lines 1, 14**

- Entire package is a stub
- `IncrementCounter()` does nothing - should send to DataDog statsd
- **Impact**: No DataDog metrics emitted

## Summary by Impact

### 🔴 Critical (Breaks Core Features)

1. MCP server tool registration and serving
2. Tunnel client creation
3. Vercel file collection
4. Manager activity handling and WebSocket URL construction

### 🟡 Medium (Degrades Features)

1. Command flag wiring (session mode, input format, skip git config, etc.)
2. Activity recorder and telemetry plumbing
3. Session ingress log submission
4. Claude executor environment setup
5. Git proxy URL construction

### 🟢 Low (Missing Telemetry/Type Safety)

1. All duration metrics and o11y timing
2. Type recovery (interface{} → concrete types)
3. DataDog metrics (stub package)
4. MCP registration error handling
5. OTel context propagation

## Verification Strategy

To validate completeness:

1. **Differential Testing**: Run reconstructed binary vs original with identical inputs
2. **Coverage Analysis**: Compare function call graphs from both binaries
3. **Integration Tests**: Exercise each stub and verify behavior matches expectations
4. **Binary Diffing**: Compare execution traces for critical paths

## Next Steps

Priority order for completion:

1. Fix MCP tool registration (enables core MCP functionality)
2. Implement tunnel client creation (enables remote development)
3. Complete Vercel CollectFiles (enables deployment actions)
4. Wire command flags to actual config
5. Add metrics/telemetry (last - non-functional)
