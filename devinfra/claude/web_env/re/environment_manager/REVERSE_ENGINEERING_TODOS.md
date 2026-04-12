# Environment Manager Reverse Engineering - Remaining Work

Binary: Build ID `495ea204`, version `release-d84d76b7-ext`

**Status:** Binary diff (2026-03-26) revealed major code changes from a6f96673.
The source in `src/` contains dead code from removed features. All previously
missing source files have been created.

## Critical Constraint: Binary Is Garble-Obfuscated

The 495ea204 binary is obfuscated using garble (Go obfuscator):

- `go version -m` returns "unknown" -- module info stripped
- `go tool nm` returns no output -- symbol table garbled
- No DWARF debug info present
- Binary size doubled (27 MB -> 49 MB) from inlining and padding
- All function/type names replaced with random identifiers
- `CLAUDE_CODE_*` env var constants are obfuscated in the string table

**DWARF-based reconstruction (as done for a6f96673) is impossible.**

## Binary Diff Findings (2026-03-26)

See `BINDIFF_RESULTS.md` for full analysis. Key findings:

### Removed from 495ea204 (vs a6f96673)

1. **Supabase MCP server** -- entire package excised (0 of 199 strings remain)
2. **Vercel deploy backend** -- removed (0 of 32 strings remain)
3. **Antspace deploy backend** -- removed (0 of 42 strings remain)
4. **Baku project features** -- initialization, templates, settings (1 of 34 strings remain)

### Added in 495ea204

- `filestore_url`, `filesystem_id` JSON fields (new deploy mechanism)
- `jwt` JSON field (auth-related)

### Unchanged

- V0/V1 session context struct layouts
- API endpoint paths (minus Supabase provision)
- CLI flags and sandbox settings
- Heartbeat/lease response structure

## ~~Priority 1: Remove Dead Code from `src/`~~ -- DONE

All dead code from removed features has been removed from `src/`:

- Deleted: `internal/mcp/servers/supabase/` (client.go, registration.go, server.go)
- Deleted: `internal/tunnel/actions/deploy/vercel.go`
- Deleted: `internal/tunnel/actions/deploy/antspace.go`
- Cleaned: `internal/auth/context.go` -- Supabase/Vercel/Antspace fields and methods removed
- Cleaned: `internal/manager/mcp.go` -- Supabase MCP server registration removed
- Cleaned: `internal/envtype/anthropic/anthropic.go` -- Baku functions removed
- Cleaned: `internal/envtype/anthropic/skill_content.go` -- Baku embedded content removed

## ~~Priority 2: Add Discovered JSON Fields to RE Source~~ -- DONE

Key struct updates applied:

- `StartupContext` / `startupContextJSON`: added `use_code_sessions`, `use_sandbox_gateway_config`,
  `custom_system_prompt`, `append_system_prompt`, `model`, `allowed_tools`, `disallowed_tools`,
  `enabled_tools`, `environment_sub_type`, `entrypoint`, `filestore_url`, `filesystem_id`, `worker_id`
- `WorkerEpoch` corrected to `int64` with `json:"worker_epoch,string,omitempty"` (was `string`)
- Lease heartbeat response struct in `lease_manager.go`: added `lease_extended`, `state`,
  `last_heartbeat`, `ttl_seconds`, `lease_updated_at`
- `jwt` field added to auth context

Note: `internal/envtype/shared/` package (embedded settings JSON, stop hook scripts) is
currently inlined in `skill_content.go`. In the actual source this is a separate `shared`
package used by both `anthropic` and `byoc` env types — splitting is still pending.

## ~~Priority 3: Update Deploy Action for Filestore~~ -- DONE

`internal/tunnel/actions/deploy/action.go` updated to use `filestore_url` and
`filesystem_id`. Actual upload logic is garble-obfuscated and cannot be recovered
without runtime observation of a live deployment.

## ~~Priority 4: Investigate `jwt` Auth Field~~ -- DONE

The `json:"jwt"` field has been added to the auth context struct. Its exact purpose
(new auth mechanism vs internal API response) remains unclear from the obfuscated binary.

## Known Gaps in the Source

- **`internal/envtype/shared/` package**: Embedded content (settings JSON, stop hook
  scripts) currently inlined in `skill_content.go`; should be a separate package
- **Obfuscated env vars**: `CLAUDE_CODE_*` constants are garbled in the new binary
- **Stale binary addresses**: All `0x...` addresses in comments are from a6f96673
- **TODO(re) markers**: Remaining markers document garble-obfuscated logic that
  cannot be recovered without runtime observation

## What Was Verified (2026-03-26)

- CLI flags: all subcommands identical flags and defaults
- Sandbox settings: `enableWeakerNestedSandbox: false`, same domain lists
- Version string: `release-d84d76b7-ext`
- V0/V1 struct layouts: field-by-field match via garbled type recovery
- Removed features: confirmed via string count comparison (HIGH confidence)
- New fields: `filestore_url`, `filesystem_id`, `jwt` (HIGH confidence)
