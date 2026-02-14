# environment-manager RE Plan

Detailed reconstruction plan for `environment-manager` binary
(Build ID `6b49f1ca37d9bf02f0b899a4a845ce551dcbcf14`).

## Situation Assessment

Unlike `process_api` (stripped Rust binary requiring Ghidra decompilation),
`environment-manager` is a Go binary **with full debug info and symbols**.
This means:

- **No decompilation needed**: We have function names, types, source file paths
- **DWARF line tables** map binary addresses → exact source file + line number
- **Go `objdump`** can produce annotated disassembly with source references
- **Symbol table** gives us every function address and size
- **`go version -m`** reveals all dependencies and build flags

The reconstruction approach shifts from "decompilation" to "disassembly-guided
source reconstruction" — reading the disassembled Go from known function
boundaries and translating to idiomatic Go source.

## Phase 0: Analysis (COMPLETE)

- [x] Binary identification (Go 1.25.6, version `staging-7c3cd5476`)
- [x] DWARF source file extraction (78 application Go files)
- [x] Symbol table extraction (691 functions with addresses)
- [x] Dependency list extraction (30+ Go modules)
- [x] Embedded content extraction (3 install scripts, hook templates)
- [x] CLI flag identification (20 flags across 6 subcommands)
- [x] API endpoint identification (6 endpoints)
- [x] Application string extraction (309 log messages)
- [x] Architecture documentation

## Phase 1: Go Module Scaffold

Create the Go module structure matching the original package layout.

- [ ] Create `go.mod` with `github.com/anthropics/anthropic/api-go/environment-manager`
      module path (or a local equivalent)
- [ ] Create all 28 package directories matching DWARF source tree
- [ ] Add `go.sum` with dependency hashes from binary metadata
- [ ] Create BUILD.bazel with `go_binary` target
- [ ] Verify `go build` produces an empty binary with correct structure

### Package Priority Order

Reconstruct packages bottom-up (leaves first, then dependents):

**Tier 1 — Leaf packages (no internal deps):**

1. `internal/util` — lockfile, retry, git helpers, periodic invoker, tailer
2. `internal/logger` — file logger, multi-handler, log writer
3. `internal/config` — config types, session mode
4. `internal/input` — V0/V1 input parsers, work secret
5. `internal/auth` — auth context, GitHub source provider
6. `internal/process` — process_api WebSocket client

**Tier 2 — Mid-level packages:**

7. `internal/api` — HTTP client, session ingress, work client
8. `internal/o11y` — observability service, OTel + DataDog
9. `internal/o11y/diag` — diagnostic logs, CC log collector
10. `internal/sandbox` — config, install, runtime wrapper
11. `internal/claude` — install, execute, outcomes, init
12. `internal/sources` — git handler, source manager
13. `internal/gitproxy` — git HTTP proxy server
14. `internal/podmonitor` — lease manager
15. `internal/session` — activity recorder

**Tier 3 — Integration packages:**

16. `internal/envtype` — environment type interface
17. `internal/envtype/anthropic` — Anthropic environment
18. `internal/envtype/byoc` — BYOC environment
19. `internal/mcp` — MCP registry, base server
20. `internal/mcp/servers/codesign` — code-sign MCP server
21. `internal/tunnel` — tunnel client, HTTP/WS handlers
22. `internal/tunnel/actions` — action registry
23. `internal/tunnel/actions/deploy` — Vercel deploy action

**Tier 4 — Top-level packages:**

24. `internal/manager` — session manager, MCP, tunnel registration
25. `internal/orchestrator` — orchestrator, hooks, poller, whoami
26. `cmd/` — all 6 Cobra commands
27. `main.go` — entry point

**Shared dependency:**

28. `core/dogmetrics` — DataDog metrics (separate internal module)
29. `gen/proto/anthropic/sessions/tunnel/v1alpha` — tunnel protobuf

## Phase 2: Per-Package Reconstruction

For each package, the approach is:

1. **List functions** from symbol table (already extracted)
2. **Disassemble** each function with `go tool objdump -S`
3. **Extract type definitions** from DWARF info
4. **Reconstruct Go source** guided by disassembly + types + strings
5. **Verify** function compiles and matches expected signature

### Per-Function Workflow

```bash
# Disassemble a single function with source annotations
go tool objdump -S -s 'internal/util.AcquireLock' environment-manager

# Get DWARF type info
readelf --debug-dump=info environment-manager | grep -A20 'AcquireLock'
```

### Estimated Effort Per Tier

| Tier      | Packages | Functions | Est. LoC    | Difficulty  |
| --------- | -------- | --------- | ----------- | ----------- |
| Tier 1    | 6        | ~60       | ~2,000      | Low         |
| Tier 2    | 9        | ~250      | ~7,000      | Medium      |
| Tier 3    | 8        | ~200      | ~5,000      | Medium-High |
| Tier 4    | 3        | ~100      | ~4,000      | Medium      |
| Shared    | 2        | ~24       | ~800        | Low         |
| **Total** | **28**   | **~634**  | **~19,000** | —           |

## Phase 3: Protobuf Reconstruction

The tunnel protocol uses protobuf. The `.proto` file can be partially
reconstructed from:

- Protobuf descriptor embedded in the binary (`.pb.go` file)
- gRPC service method names from symbol table
- Message field names from serde/JSON tags in the binary

Target: `anthropic/sessions/tunnel/v1alpha/tunnel.proto`

Known messages:

- `TunnelRequest` (oneof: `HttpRequest`, `HttpCancel`, `WsOpen`, `WsClose`, `WsMessage`)
- `TunnelResponse` (oneof: `HttpChunk`, `HttpError`, `HttpHeaders`, `WsOpened`, `WsClose`, `WsError`, `WsMessage`)
- `HTTPTunnelRequest`, `HTTPTunnelResponseChunk`, `HTTPTunnelResponseHeaders`, `HTTPTunnelResponseError`, `HTTPTunnelCancel`
- `WSTunnelOpen`, `WSTunnelOpened`, `WSTunnelClose`, `WSTunnelError`, `WSTunnelMessage`
- `Header`

## Phase 4: Embedded Content Extraction

- [x] Language install scripts (Python, Node.js, Go)
- [ ] Claude Code hook templates (session-start, stop hooks)
- [ ] Cobra completion templates (bash, zsh, fish, PowerShell)
- [ ] Default sandbox configuration JSON
- [ ] MCP server configuration template

## Phase 5: Build & Verify

1. **Compile**: `go build` or `bazel build` produces a binary
2. **Symbol diff**: Compare symbol tables (function names, sizes)
3. **String diff**: Compare application strings
4. **Behavioral test**: Run against the same test harness as `process_api`
5. **Integration test**: Full orchestrator flow with mock API

## Phase 6: Documentation

- [ ] Update `README.md` with discovered implementation details
- [ ] Document the tunnel protobuf protocol
- [ ] Document the session lifecycle state machine
- [ ] Document the API authentication flow
- [ ] Cross-reference with `process_api` for the full container boot sequence

## Key Open Questions

1. **Tunnel protocol**: Is the gRPC tunnel service defined in a shared `.proto`
   file, or is it specific to environment-manager? The proto package
   `anthropic.sessions.tunnel.v1alpha` suggests it's shared infrastructure.

2. **BYOC auth**: The `containProvideAuthRoundTripper` in the BYOC environment
   type — what authentication scheme does it implement?

3. **MCP code-sign**: The code-sign MCP server acts as a GPG/SSH signing
   wrapper. How does it interact with the git proxy for signed commits?

4. **Internal API module**: `api-go/core/dogmetrics` is a separate internal
   module — it likely wraps the DataDog client with Anthropic-specific
   conventions. Only 2 exported functions (`Distribution`, `Incr`).

5. **Session modes**: `config.ParseSessionMode` suggests multiple session
   modes. What are they and how do they affect the orchestrator loop?
