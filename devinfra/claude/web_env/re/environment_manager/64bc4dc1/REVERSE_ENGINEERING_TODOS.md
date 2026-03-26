# Environment Manager Reverse Engineering - Remaining Work

Binary: Build ID `64bc4dc1`, version `release-9f4ec76fbc-ext`

**Status:** Binary diff (2026-03-26) revealed major code changes from a6f96673.
The source in `src/` contains dead code from removed features and is missing
several files identified in the old binary's source paths.

## Critical Constraint: Binary Is Garble-Obfuscated

The 64bc4dc1 binary was obfuscated using garble (Go obfuscator):

- `go version -m` returns "unknown" -- module info stripped
- `go tool nm` returns no output -- symbol table garbled
- No DWARF debug info present
- Binary size doubled (27 MB -> 49 MB) from inlining and padding
- All function/type names replaced with random identifiers
- `CLAUDE_CODE_*` env var constants are obfuscated in the string table

**DWARF-based reconstruction (as done for a6f96673) is impossible.**

## Binary Diff Findings (2026-03-26)

See `BINDIFF_RESULTS.md` for full analysis. Key findings:

### Removed from 64bc4dc1 (vs a6f96673)

1. **Supabase MCP server** -- entire package excised (0 of 199 strings remain)
2. **Vercel deploy backend** -- removed (0 of 32 strings remain)
3. **Antspace deploy backend** -- removed (0 of 42 strings remain)
4. **Baku project features** -- initialization, templates, settings (1 of 34 strings remain)

### Added in 64bc4dc1

- `filestore_url`, `filesystem_id` JSON fields (new deploy mechanism)
- `jwt` JSON field (auth-related)

### Unchanged

- V0/V1 session context struct layouts
- API endpoint paths (minus Supabase provision)
- CLI flags and sandbox settings
- Heartbeat/lease response structure

## Priority 1: Remove Dead Code from `src/` (HIGH)

The binary diff proves these source files represent code no longer in the binary.
They must be removed to avoid misleading future RE work.

### Files to delete

- `internal/mcp/servers/supabase/client.go`
- `internal/mcp/servers/supabase/registration.go`
- `internal/mcp/servers/supabase/server.go`
- `internal/tunnel/actions/deploy/vercel.go`
- `internal/tunnel/actions/deploy/antspace.go`

### Code to remove from existing files

- `internal/auth/context.go`: Remove `supabaseAnonKey`, `supabaseDBPass`,
  `supabasePAT`, `supabaseProjectRef` fields and `HasSupabase()`,
  `GetSupabaseAnonKey()`, `GetSupabasePAT()`, `GetSupabaseDBPass()`,
  `GetSupabaseProjectRef()` methods. Remove `vercelDeployToken`,
  `antspaceAuthToken`, `antspaceControlPlaneURL` fields and their getters.
- `internal/manager/mcp.go`: Remove Supabase MCP server registration.
- `internal/envtype/anthropic/anthropic.go`: Remove `findExistingBakuProject()`,
  `initializeBakuProject()`, `bootstrapBakuSettings()` functions and Baku
  template paths (`/opt/baku-templates/vite-template`,
  `/home/claude/project/.baku/explorations`, `/home/claude/project/.baku/drafts`).
- `internal/envtype/anthropic/skill_content.go`: Remove Baku-specific embedded
  content (stop-hook-baku.sh, baku settings JSON).

## Priority 2: Add Missing Source Files (MEDIUM)

The old binary's DWARF paths include 12 source files not represented in `src/`.
These files exist in both old and new binaries (they are not removed features).

| File to create                               | Known content                                                                               |
| -------------------------------------------- | ------------------------------------------------------------------------------------------- |
| `internal/api/ccr_backend.go`                | CCR v2 backend (`RegisterWorker`, `WorkerEpoch`, `OtlpEndpoints`, `FlushLogs`, `PostEvent`) |
| `internal/api/noop_backend.go`               | No-op backend for `setup-only` session mode                                                 |
| `internal/api/session_ingress_backend.go`    | Session ingress backend (`OtlpEndpoints`, `FlushLogs`, `PostEvent`)                         |
| `internal/claude/session_urls.go`            | `buildSessionURLs()` function                                                               |
| `internal/envtype/anthropic/config.go`       | Anthropic environment config types                                                          |
| `internal/input/parser.go`                   | Input parser interface                                                                      |
| `internal/manager/skill_extraction.go`       | Skill extraction from repos                                                                 |
| `internal/mcp/servers/codesign/types.go`     | Code-sign type definitions                                                                  |
| `internal/session/noop_activity_recorder.go` | No-op activity recorder implementation                                                      |
| `internal/util/net.go`                       | Network utilities                                                                           |
| `internal/util/stream.go`                    | Stream utilities (streamer)                                                                 |

Note: `internal/envtype/shared/` package contains embedded content (settings JSON,
stop hook scripts) that is currently in `skill_content.go`. In the actual source
this is a separate `shared` package used by both `anthropic` and `byoc` env types.

## Priority 3: Update Deploy Action for Filestore (LOW)

`internal/tunnel/actions/deploy/action.go` now uses `filestore_url` and
`filesystem_id` instead of Vercel/Antspace. The actual logic is fully garbled
and cannot be recovered without runtime observation of a live deployment.

## Priority 4: Investigate `jwt` Auth Field (LOW)

The new `json:"jwt"` field in the binary is not part of the V0/V1 session context
structs. It may be part of a new auth mechanism or internal API response. Location
in the code is unknown.

## Known Gaps in the Source

The source contains 27 `TODO(re)` markers across 8 files (inherited from the
a6f96673 reconstruction). With the binary diff findings, additional gaps are now
documented:

- **Dead Supabase code**: 3 source files that should be deleted
- **Dead Vercel/Antspace code**: 2 source files that should be deleted
- **Dead Baku code**: Functions and embedded content that should be removed
- **Missing source files**: 12 files identified from old binary DWARF paths
- **New deploy mechanism**: `filestore_url`/`filesystem_id` logic is unknown
- **Obfuscated env vars**: `CLAUDE_CODE_*` constants are garbled in new binary
- **Stale binary addresses**: All `0x...` addresses in comments are from a6f96673

## What Was Verified (2026-03-26)

- CLI flags: all subcommands identical flags and defaults
- Sandbox settings: `enableWeakerNestedSandbox: false`, same domain lists
- Version string: `release-9f4ec76fbc-ext`
- V0/V1 struct layouts: field-by-field match via garbled type recovery
- Removed features: confirmed via string count comparison (HIGH confidence)
- New fields: `filestore_url`, `filesystem_id`, `jwt` (HIGH confidence)
