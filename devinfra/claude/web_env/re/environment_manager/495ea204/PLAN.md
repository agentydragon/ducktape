# environment-manager RE Plan (64bc4dc1)

Reconstruction plan for `environment-manager` binary
(Build ID `64bc4dc1a5a3a38ce5732655f7fdfbeb62b8598d`).

## Binary Summary

| Property             | Value                                                                    |
| -------------------- | ------------------------------------------------------------------------ |
| Build ID             | `64bc4dc1a5a3a38ce5732655f7fdfbeb62b8598d`                               |
| Version              | `release-9f4ec76fbc-ext`                                                 |
| Channel              | `release` (production, was `staging` in a6f96673)                        |
| Go version           | Unknown (garble strips module info -- `go version -m` returns "unknown") |
| Binary size          | 49 MB (was 27 MB -- garble inlines and pads code)                        |
| Anthropic functions  | N/A (symbol table garbled -- `go tool nm` returns no output)             |
| Source files (DWARF) | N/A (no DWARF debug info -- garble strips it)                            |
| Obfuscation          | garble (Go obfuscator) -- all symbol names garbled                       |

## Binary Diff Results (2026-03-26)

**The RE assumption that "nothing changed except obfuscation" is false.**

The binary diff between a6f96673 (symbols) and 64bc4dc1 (garbled) reveals major
application-level changes. See `BINDIFF_RESULTS.md` for full details.

### Removed Features

1. **Supabase MCP server** -- entire `internal/mcp/servers/supabase/` removed
   (199 string matches in old, 0 in new). All Supabase auth fields, MCP tools
   (`provision_database`, `deploy_function`, `list_migrations`, `apply_migration`,
   `generate_types`), and API endpoints removed.

2. **Vercel deploy** -- `internal/tunnel/actions/deploy/vercel.go` removed
   (32 string matches in old, 0 in new). Deploy tokens and Vercel-specific
   response fields removed.

3. **Antspace deploy** -- `internal/tunnel/actions/deploy/antspace.go` removed
   (42 string matches in old, 0 in new). Control plane URL, auth token, and
   Antspace-specific error messages removed.

4. **Baku project features** -- Baku initialization, template copying, and
   settings bootstrap functions removed (34 string matches in old, 1 garbled in
   new). Paths `/opt/baku-templates/vite-template`,
   `/home/claude/project/.baku/{explorations,drafts}` removed.

### New Features

- `json:"filestore_url"` and `json:"filesystem_id"` -- new deploy mechanism
  fields, likely replacing Vercel/Antspace.
- `json:"jwt"` -- new auth field.

### Unchanged

- V0 and V1 session context struct layouts (all fields match).
- API endpoint paths (same set, minus Supabase provision endpoint).
- CLI flags and subcommands (identical `--help` output).
- Sandbox settings (identical `print-sandbox-settings` output).
- Heartbeat response structure.

## Source Accuracy

The source in `src/` was carried forward from a6f96673 and requires updates:

### Files to REMOVE from `src/` (dead code in 64bc4dc1)

- `internal/mcp/servers/supabase/` (all files: `client.go`, `registration.go`, `server.go`)
- `internal/tunnel/actions/deploy/vercel.go`
- `internal/tunnel/actions/deploy/antspace.go`

### Files to UPDATE in `src/`

- `internal/auth/context.go` -- remove Supabase fields (`supabaseAnonKey`,
  `supabaseDBPass`, `supabasePAT`, `supabaseProjectRef`, `HasSupabase()`) and
  Vercel/Antspace fields (`vercelDeployToken`, `antspaceAuthToken`,
  `antspaceControlPlaneURL`)
- `internal/tunnel/actions/deploy/action.go` -- update to use `filestore_url`
  and `filesystem_id` instead of Vercel/Antspace
- `internal/envtype/anthropic/anthropic.go` -- remove Baku-specific functions
  (`findExistingBakuProject`, `initializeBakuProject`, `bootstrapBakuSettings`)
- `internal/manager/mcp.go` -- remove Supabase MCP server registration

### Missing files (in old binary but not in `src/`)

| Path in old binary                            | Status  |
| --------------------------------------------- | ------- |
| `internal/api/ccr_backend.go`                 | Missing |
| `internal/api/noop_backend.go`                | Missing |
| `internal/api/session_ingress_backend.go`     | Missing |
| `internal/claude/session_urls.go`             | Missing |
| `internal/envtype/anthropic/config.go`        | Missing |
| `internal/envtype/shared/` (embedded content) | Missing |
| `internal/input/parser.go`                    | Missing |
| `internal/manager/skill_extraction.go`        | Missing |
| `internal/mcp/servers/codesign/types.go`      | Missing |
| `internal/session/noop_activity_recorder.go`  | Missing |
| `internal/util/net.go`                        | Missing |
| `internal/util/stream.go`                     | Missing |

## Obfuscation Evidence

- `go version -m` returns "unknown" -- garble strips module info
- `go tool nm` returns no output -- symbol table is garbled
- Obfuscated names visible in strings: `qbbw3lR`, `pVHE5Urql8v`, `gDCX1skL`, etc.
- Binary size doubled: 27 MB -> 49 MB (garble inlines and pads code)
- Version string `release-9f4ec76fbc-ext` visible as a literal constant (not obfuscated)
- JSON field names and error strings are still visible (runtime string literals)
- `CLAUDE_CODE_*` env var names are obfuscated (20+ vars in old, ~5 references in new)

## Verified CLI Behavior (from runtime analysis)

All CLI flags and subcommands verified unchanged from a6f96673 via `--help` output:

- `orchestrator`, `setup`, `task-run`, `poll`, `print-sandbox-settings`, `completion`
- All flags identical to a6f96673 (same names, defaults, descriptions)

## Verified Sandbox Settings (from print-sandbox-settings)

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

## New OTEL/Telemetry Strings (from dependency updates)

- `gcp.apphub.service.criticality_type`, `gcp.apphub.service.environment_type`
- `gcp.apphub.workload.criticality_type`, `gcp.apphub.workload.environment_type`
- `feature_flag.evaluation.reason`, `feature_flag.result.reason`
- `grpc.internal.transport.networktype`, `grpc.internal.address.metadata`
- `rpc.connect_rpc.error_code`
- New runtime metrics: `/cpu/classes/`, `/gc/cycles/`, `/sched/pauses/`

These come from updated gRPC and OTel dependency versions, not application-level changes.

## Phase Status

### Phase 1: Census & Diff -- COMPLETE

- [x] Confirmed obfuscation via `go version -m`, `go tool nm`
- [x] Binary size: 49 MB confirmed
- [x] Version string: `release-9f4ec76fbc-ext` confirmed
- [x] CLI flags verified via `--help` (all subcommands)
- [x] Sandbox settings verified via `print-sandbox-settings`
- [x] String diff against a6f96673 for new OTEL attributes
- [x] Full binary diff string analysis (2026-03-26) -- see `BINDIFF_RESULTS.md`
- [x] Identified removed features: Supabase, Vercel, Antspace, Baku
- [x] Identified new features: `filestore_url`, `filesystem_id`, `jwt`
- [x] Struct layout comparison via garbled type recovery

### Phase 2: Source Updates -- IN PROGRESS

- [x] Updated BUILD.bazel files (replaced a6f96673 paths with 64bc4dc1)
- [x] Updated Go source file headers to reflect provenance (a6f96673 DWARF origin)
- [x] Updated main.go version string and obfuscation notice
- [x] Updated PLAN.md, REVERSE_ENGINEERING_TODOS.md, BINDIFF_RESULTS.md
- [ ] Remove dead Supabase source files from `src/`
- [ ] Remove dead Vercel/Antspace source files from `src/`
- [ ] Update `auth/context.go` to remove Supabase/Vercel/Antspace fields
- [ ] Update `tunnel/actions/deploy/action.go` for new filestore mechanism
- [ ] Remove Baku-specific functions from `anthropic.go`
- [ ] Add missing source files identified from old binary paths
- [ ] DWARF-based reconstruction (IMPOSSIBLE -- binary is garble-obfuscated)

## Open Items

1. **New deploy mechanism**: `filestore_url` and `filesystem_id` suggest a new
   deployment backend replacing Vercel/Antspace. Logic is fully garbled.
2. **Dependency versions**: Cannot extract from garbled binary; go.mod reflects a6f96673.
3. **Missing source files**: 12 files in old binary paths not represented in `src/`.
