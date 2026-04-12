# Environment Manager Reverse Engineering - Remaining Work

Binary: Build ID `495ea204`, version `release-d84d76b7-ext`

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

## Remaining Work

- **Obfuscated env vars**: `CLAUDE_CODE_*` constants are garbled in the new binary
- **Stale binary addresses**: All `0x...` addresses in comments are from a6f96673
- **New deploy mechanism**: `filestore_url`/`filesystem_id` logic is fully garbled;
  cannot be recovered without runtime observation of a live deployment
- **TODO(re) markers**: Remaining markers document garble-obfuscated logic that
  cannot be recovered without runtime observation
- **`json:"mount_path,omitempty"` field**: Present in binary RTTI but not yet placed
  in any source struct. Likely in a filesystem/container configuration struct.
- **`json:"organization_uuid"` field**: Present in binary RTTI but not yet in any
  JSON struct; currently only used as an HTTP header (`organization_uuid` header in
  lease heartbeat). May also be in a JSON request/response struct not yet identified.
