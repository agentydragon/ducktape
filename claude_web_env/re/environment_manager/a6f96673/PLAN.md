# environment-manager RE Plan (a6f96673)

Reconstruction plan for `environment-manager` binary
(Build ID `a6f96673c2497a946dc0797780b5c6df47c0946e`).

## Binary Summary

| Property             | Value                                      |
| -------------------- | ------------------------------------------ |
| Build ID             | `a6f96673c2497a946dc0797780b5c6df47c0946e` |
| Version              | `staging-68f0dff496`                       |
| Go version           | 1.25.7                                     |
| Binary size          | 27.3 MB                                    |
| Anthropic functions  | 949                                        |
| Source files (DWARF) | ~82                                        |

## Dependencies

| Module                       | Version  |
| ---------------------------- | -------- |
| Go compiler                  | 1.25.7   |
| `connectrpc.com/connect`     | v1.19.1  |
| `google.golang.org/grpc`     | v1.79.0  |
| `go.opentelemetry.io/otel`   | v1.39.0  |
| `google.golang.org/protobuf` | v1.36.11 |
| `cenkalti/backoff/v5`        | v5.0.3   |

## Phase 1: Census & Diff (COMPLETE)

- [x] Extract symbol table (949 Anthropic functions)
- [x] Extract all application strings (144,645 strings)
- [x] Compare packages against old source tree
- [x] Identify new packages: `tunnel/actions/snapshot`, `tunnel/actions/status`
- [x] Identify new files: `deploy/antspace.go`
- [x] Identify removed subcommand: `code-sign` AddCommand (now inlined)
- [x] Identify added subcommand: `completion` (Cobra built-in)

## Phase 2: Full Disassembly-Guided Reconstruction (COMPLETE)

### New Packages/Files Created

1. [x] `internal/tunnel/actions/snapshot/action.go` — NEW package
   - `SnapshotAction` with Execute, readDir, readFileSafe, gitCommitCount, gitAppModified
   - Types: `fileEntry`, `snapshotResponse`
   - Binary addresses: 0xba1a20 (Name), 0xba1a40 (Timeout), 0xba1a60 (Execute),
     0xba20c0 (readDir), 0xba2880 (readFileSafe), 0xba3120 (gitCommitCount),
     0xba33a0 (gitAppModified)

2. [x] `internal/tunnel/actions/status/action.go` — NEW package
   - `StatusAction` with Execute, Name, Timeout
   - Types: `statusResult`
   - Binary addresses: 0xba3a20 (Name), 0xba3a40 (Timeout), 0xba3a60 (Execute)

3. [x] `internal/tunnel/actions/deploy/antspace.go` — NEW file
   - `AntspaceClient` with Deploy, readDeployResponse
   - Types: `AntspaceDeployResult`, `deployRequest`
   - Binary addresses: 0xb9dc00 (Deploy), 0xb9e1c0 (readDeployResponse)

### Updated Files

4. [x] `main.go` — Major restructuring
   - `util.Version = Version` assignment
   - Basename detection for direct code-sign mode
   - code-sign and print-sandbox-settings inlined (not via AddCommand)
   - `completion` from Cobra built-in

5. [x] `cmd/cmd_code_sign.go` — Added `RunCodeSignFromMain()` exported wrapper

6. [x] `cmd/cmd_print_sandbox.go` — Added `RunPrintSandboxSettings()` exported wrapper

7. [x] `internal/tunnel/actions/deploy/action.go` — Added `executeAntspace()` method
       and `CreateTarball()` stub

8. [x] `internal/auth/context.go` — Added Antspace auth support
   - New fields: `antspaceControlPlaneURL`, `antspaceAuthToken` (offsets 0x40, 0x50)
   - New type: `antspaceDeployConfig` (JSON: url, auth_token)
   - New case: `"antspace_deploy"` in NewAuthContextWithSessionID
   - New getters: `GetAntspaceControlPlaneURL()` (0x8319c0), `GetAntspaceAuthToken()` (0x8319e0)

### Build ID Reference Updates

9. [x] Updated all Build ID references to `a6f96673` across all source files

## Phase 3: Apply Known Changes (PARTIAL)

- [x] New tunnel actions (snapshot, status)
- [x] Antspace deployment client
- [x] Antspace auth context
- [x] Main.go restructuring
- [x] Code-sign/print-sandbox inlining
- [ ] Full CLI flag audit for task-run (new flags: --claude-agent-version, --claude-path,
      --local-append-system-prompt, --verbose-claude-logs, --upgrade-claude-code)
- [ ] Full CLI flag audit for orchestrator (new flags TBD)
- [ ] Full CLI flag audit for setup (new flags TBD)
- [ ] Verify codesign MCP server still present (symbols exist)
- [ ] CreateTarball full implementation (currently stub)

## Phase 4: Documentation (COMPLETE)

- [x] Update PLAN.md with a6f96673 specifics
- [x] Update parent README.md with version table and changes
- [x] Document new Antspace deployment system
- [x] Document new tunnel actions (snapshot, status)
- [x] Document dependency version changes

## Key Findings

### Antspace Deployment System (NEW)

Alternative to Vercel deployment. Uses Anthropic's Antspace control plane:

- `AntspaceClient.Deploy()` uploads tarball via multipart POST to
  `{controlPlaneURL}/projects/{slug}/functions/deploy?slug={slug}`
- Auth via `antspace_deploy` config type (JSON: `{"url": "...", "auth_token": "..."}`)
- Error: "neither vercel_deploy token nor antspace_deploy credentials found in auth context"
- Controlled by `antspaceControlPlaneURL` and `antspaceAuthToken` in AuthContext

### Snapshot Action (NEW)

Project state snapshot for tunnel system:

- Reads directory listings from project and home dirs
- Counts git commits, checks for modifications
- Returns `snapshotResponse` with file lists, git state, truncation info

### Status Action (NEW)

Simple health check for tunnel system:

- Parses port from request (default 3000)
- Validates port <= 65534
- Returns status with "ok" message

### AuthContext Struct Layout (Updated)

```
offset 0x00: sessionIngressToken string
offset 0x10: anthropicAPIToken string
offset 0x20: anthropicOAuthToken string
offset 0x30: vercelDeployToken string
offset 0x40: antspaceControlPlaneURL string  ← NEW
offset 0x50: antspaceAuthToken string        ← NEW
offset 0x60: supabaseProjectRef string
offset 0x70: supabaseAnonKey string
offset 0x80: supabaseDBPass string
offset 0x90: supabasePAT string
offset 0xa0: sessionID string
offset 0xb0: logger *slog.Logger
```

## Open Items

1. **CreateTarball**: Full disassembly pending — currently a stub returning error
2. **CLI flag delta**: Need systematic comparison of all flags across subcommands
3. **Orchestrator changes**: May have new hook types or configuration options
4. **MCP codesign**: Symbols still present — verify if package unchanged or updated
