# environment-manager RE Plan

Detailed reconstruction plan for `environment-manager` binary
(Build ID `6b49f1ca37d9bf02f0b899a4a845ce551dcbcf14`).

## Situation Assessment

Unlike `process_api` (stripped Rust binary requiring Ghidra decompilation),
`environment-manager` is a Go binary **with full debug info and symbols**.
This means:

- **No decompilation needed**: We have function names, types, source file paths
- **DWARF line tables** map binary addresses to exact source file + line number
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

## Phase 1: Go Module Scaffold (COMPLETE)

- [x] Created `go.mod` with module path
- [x] Created all 27 package directories matching DWARF source tree
- [x] Created `main.go` entry point with version flag
- [x] Created placeholder BUILD.bazel

## Phase 2: Per-Package Reconstruction (COMPLETE)

All 81 Go files reconstructed across 28 packages (~20,800 lines).

### Tier 1 — Leaf packages (COMPLETE)

1. [x] `internal/util` — lockfile, retry, git helpers, periodic invoker, tailer (5 files)
2. [x] `internal/logger` — file logger, multi-handler, log writer (3 files)
3. [x] `internal/config` — config types, session mode (2 files)
4. [x] `internal/input` — V0/V1 input parsers, work secret (3 files)
5. [x] `internal/auth` — auth context, GitHub source provider (2 files)
6. [x] `internal/process` — process execution, script helper (2 files)

### Tier 2 — Mid-level packages (COMPLETE)

7. [x] `internal/api` — HTTP client, session ingress, work client, retry (6 files)
8. [x] `internal/o11y` — observability service, OTel + DataDog (7 files)
9. [x] `internal/o11y/diag` — diagnostic logs, CC log collector (2 files)
10. [x] `internal/sandbox` — config, install, runtime wrapper (3 files)
11. [x] `internal/claude` — install, execute, outcomes, init, config (5 files)
12. [x] `internal/sources` — git handler, source manager (2 files)
13. [x] `internal/gitproxy` — git HTTP proxy server (3 files)
14. [x] `internal/podmonitor` — lease manager (1 file)
15. [x] `internal/session` — activity recorder (1 file)

### Tier 3 — Integration packages (COMPLETE)

16. [x] `internal/envtype` — environment type interface (1 file)
17. [x] `internal/envtype/anthropic` — Anthropic environment (1 file)
18. [x] `internal/envtype/byoc` — BYOC environment (1 file)
19. [x] `internal/mcp` — MCP registry, base server (2 files)
20. [x] `internal/mcp/servers/codesign` — code-sign MCP server (3 files)
21. [x] `internal/tunnel` — tunnel client, HTTP/WS handlers (3 files)
22. [x] `internal/tunnel/actions` — action registry (1 file)
23. [x] `internal/tunnel/actions/deploy` — Vercel deploy action (2 files)

### Tier 4 — Top-level packages (COMPLETE)

24. [x] `internal/manager` — session manager, MCP, tunnel registration (3 files)
25. [x] `internal/orchestrator` — orchestrator, hooks, poller, whoami (5 files)
26. [x] `cmd/` — all 6 Cobra commands + types + utils (8 files)
27. [x] `main.go` — entry point

### Reconstruction Quality

Every reconstructed function is annotated with its binary address
(e.g., `// Binary: 0xb730c0`). Key functions include:

- Full DWARF struct offset annotations on type definitions
- RIP-relative LEA decoding for string constant references
- x86-64 Go register ABI annotations (AX, BX, CX parameter mapping)
- Closure capture layout documentation
- Call graph traces from disassembly

Remaining stub implementations (functions with `return nil` placeholder bodies)
exist in `cmd/cmd_code_sign.go`, `cmd/cmd_setup.go`, and `cmd/cmd_task_run.go`
where the RunE closures and helper functions have not yet been fully
reverse-engineered from the binary.

## Phase 3: Protobuf Reconstruction (NOT STARTED)

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

## Phase 4: Embedded Content Extraction (PARTIAL)

- [x] Language install scripts (Python, Node.js, Go)
- [ ] Claude Code hook templates (session-start, stop hooks)
- [ ] Cobra completion templates (bash, zsh, fish, PowerShell)
- [ ] Default sandbox configuration JSON
- [ ] MCP server configuration template

## Phase 5: Build & Verify (NOT STARTED)

1. **Compile**: `go build` or `bazel build` produces a binary
2. **Symbol diff**: Compare symbol tables (function names, sizes)
3. **String diff**: Compare application strings
4. **Behavioral test**: Run against the same test harness as `process_api`
5. **Integration test**: Full orchestrator flow with mock API

## Phase 6: Documentation (IN PROGRESS)

- [x] Update `README.md` with discovered implementation details
- [ ] Document the tunnel protobuf protocol
- [ ] Document the session lifecycle state machine
- [ ] Document the API authentication flow
- [x] Cross-reference with `process_api` for the full container boot sequence

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

5. **Session modes**: `config.ParseSessionMode` supports four modes:
   `new`, `setup-only`, `resume`, `resume-cached`. How do they affect
   the orchestrator loop?
