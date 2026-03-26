# Binary Diff Results: a6f96673 vs 64bc4dc1

Comparison of the old (`a6f96673`, 27 MB, full symbols) and new (`64bc4dc1`, 49 MB,
garble-obfuscated) environment-manager binaries.

## Method

`radiff2 -A -C` was attempted but timed out on the 50 MB binaries (>2 min). Analysis
used string-anchored matching: full string extraction from both binaries, JSON field
tag comparison, garbled struct type recovery, env var comparison, and source file path
extraction.

## Key Finding: Significant Code Changes Between Versions

**The RE assumption that "nothing changed except obfuscation" is false.** The binary
diff reveals three categories of real code changes:

### 1. Supabase MCP Server: REMOVED

The entire `internal/mcp/servers/supabase/` package was removed. Evidence:

| Indicator                | Old (a6f96673)                                                                                                                   | New (64bc4dc1) |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------- | -------------- |
| "supabase" string count  | 199                                                                                                                              | 0              |
| Source files             | 4 (client.go, functions.go, registration.go, server.go)                                                                          | 0              |
| Auth fields removed      | `anon_key`, `db_pass`, `project_ref`, `pat`                                                                                      | gone           |
| Function symbols removed | `GetSupabaseAnonKey`, `GetSupabasePAT`, etc.                                                                                     | gone           |
| MCP tools removed        | `provision_database`, `deploy_function`, `list_migrations`, `apply_migration`, `generate_types`                                  | gone           |
| Error messages removed   | "Supabase provisioning failed", "Supabase provisioning permanently failed", "max functions reached for this Supabase plan (402)" | gone           |

**Impact on RE:** The `internal/mcp/servers/supabase/` directory in `src/` is now dead
code. The `internal/auth/context.go` Supabase fields (`supabaseAnonKey`, `supabaseDBPass`,
`supabasePAT`, `supabaseProjectRef`, `HasSupabase()`) are also removed.

### 2. Vercel + Antspace Deploy: REMOVED

The Vercel and Antspace deployment backends were removed from the tunnel deploy action.

| Indicator               | Old (a6f96673)                                                                                    | New (64bc4dc1) |
| ----------------------- | ------------------------------------------------------------------------------------------------- | -------------- |
| "vercel" string count   | 32                                                                                                | 0              |
| "antspace" string count | 42                                                                                                | 0              |
| Source files removed    | `deploy/vercel.go`, `deploy/antspace.go`                                                          | gone           |
| Auth fields removed     | `vercel_deploy_token`, `antspace_auth_token`, `antspace_control_plane_url`                        | gone           |
| JSON fields removed     | `deploy_id`, `deploy_url`, `deploy_target`, `deployer`, `inspector_url`, `inspectorUrl`           | gone           |
| Error messages removed  | "vercel deployment creation failed", "Deploying %d files to antspace...", "antspace deploy error" | gone           |

**Note:** The deploy action framework (`deploy/action.go`) and `filestore_url` field
still exist. Deployment likely now routes through a different backend. The new binary
has `json:"filestore_url"` and `json:"filesystem_id"` which are new fields not present
in the old binary, suggesting a replacement deploy mechanism.

### 3. Baku Features: REMOVED OR HEAVILY OBFUSCATED

Baku-specific features were largely removed or fully obfuscated:

| Indicator                                 | Old (a6f96673) | New (64bc4dc1)     |
| ----------------------------------------- | -------------- | ------------------ |
| "baku" string count                       | 34             | 1 (garbled)        |
| `findExistingBakuProject`                 | present        | gone               |
| `bootstrapBakuSettings`                   | present        | gone               |
| `initializeBakuProject`                   | present        | gone               |
| `/opt/baku-templates/vite-template`       | present        | gone (in old only) |
| `/home/claude/project/.baku/explorations` | present        | gone               |
| `/home/claude/project/.baku/drafts`       | present        | gone               |
| `_BAKU_SUPABASE_LAZY`                     | present        | gone               |
| Baku stop hook script                     | present        | gone               |

**Note:** Some Baku error strings like "Failed to bootstrap Baku settings" survive in the
old binary's concatenated string blobs, confirming these were real functions that have
been excised.

## New JSON Fields in 64bc4dc1

Three JSON fields appear in the new binary but not the old:

| Field           | Context                    |
| --------------- | -------------------------- |
| `filestore_url` | New deploy mechanism field |
| `filesystem_id` | New deploy mechanism field |
| `jwt`           | Auth-related               |

## Struct Layout Comparison

The garbled binary leaks full struct layouts through Go's type reflection system.

### V1 Session Context (StartupContext) -- UNCHANGED

Both binaries have identical V1 fields:

```
version, session_ingress_token, api_base_url, sources, auth,
claude_code_args, mcp_config, use_sandbox_gateway_config,
environment_variables, use_code_sessions, outcomes,
custom_system_prompt, append_system_prompt, cwd
```

### V0 Input (Session) -- UNCHANGED

Both binaries have identical V0 fields:

```
sources, api_base_url, outcomes, custom_system_prompt,
append_system_prompt, model, mcp_config, allowed_tools,
disallowed_tools, enabled_tools, claude_code_args,
mcp_config_file, use_sandbox_gateway_config, entrypoint,
environment_variables, environment_sub_type, use_code_sessions
```

Note: `enabled_tools` existed in both old and new (it was already in a6f96673).

### Heartbeat Response -- UNCHANGED

```
lease_extended, state, last_heartbeat, ttl_seconds, lease_updated_at
```

## Environment Variable Changes

All `CLAUDE_CODE_*` env vars that were visible as plain strings in the old binary (20+
vars including `CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR`, `CLAUDE_CODE_SESSION_ID`,
`CLAUDE_CODE_REMOTE_SESSION_ID`, etc.) are no longer visible in the new binary's string
table. Garble has obfuscated these constant strings. The env vars likely still exist
functionally but cannot be enumerated via `strings`.

## Source File Path Changes

The old binary embeds 87 source file paths via DWARF. Key differences from the RE
source tree:

### Files in old binary NOT in RE `src/`:

| Path in old binary                           | Note                                      |
| -------------------------------------------- | ----------------------------------------- |
| `internal/api/ccr_backend.go`                | CCR v2 backend (RE has `work_client.go`)  |
| `internal/api/noop_backend.go`               | No-op backend for setup-only mode         |
| `internal/api/session_ingress_backend.go`    | Session ingress backend                   |
| `internal/claude/session_urls.go`            | Session URL builder (separate file)       |
| `internal/envtype/anthropic/config.go`       | Anthropic env config                      |
| `internal/envtype/shared/` (4 embed files)   | Shared embedded content                   |
| `internal/input/parser.go`                   | Input parser interface                    |
| `internal/manager/skill_extraction.go`       | Skill extraction from repos               |
| `internal/mcp/servers/codesign/types.go`     | Code-sign type definitions                |
| `internal/mcp/servers/supabase/functions.go` | Supabase function deploy (REMOVED in new) |
| `internal/session/noop_activity_recorder.go` | No-op activity recorder                   |
| `internal/util/net.go`                       | Network utilities                         |
| `internal/util/stream.go`                    | Stream utilities                          |

### Files in RE `src/` NOT in old binary paths:

| RE file                                       | Note                                   |
| --------------------------------------------- | -------------------------------------- |
| `cmd/types.go`                                | Probably split from `utils.go`         |
| `internal/tunnel/factory.go`                  | Tunnel factory (may be in `client.go`) |
| `internal/tunnel/tunnelpb/`                   | Proto helpers (inline in binary)       |
| `internal/dogmetrics/dogmetrics.go`           | In `api-go/core/dogmetrics/` in binary |
| `internal/envtype/anthropic/skill_content.go` | In `shared/` in binary                 |

## Dependency Changes

The new binary includes Go 1.25.7+ runtime features not in the old:

- New runtime metrics: `/cpu/classes/`, `/gc/cycles/`, `/sched/pauses/`, `/memory/classes/`
- New OTel attributes: `gcp.apphub.service.*`, `gcp.apphub.workload.*`, `feature_flag.*`
- New gRPC attributes: `grpc.internal.transport.networktype`, `grpc.internal.address.metadata`, `rpc.connect_rpc.error_code`
- HTML template strings (Go `net/trace` debug UI) removed -- suggests newer Go version or build flag change

The old binary had `golang.org/x/net/trace` HTML templates embedded; the new binary does
not, suggesting either the dependency was dropped or a build flag excludes it.

## API Endpoint Changes

No changes to API endpoint paths. Both binaries use the same set:

- `/v1/environments/whoami`
- `/v1/environments/{id}/work/poll`
- `/v1/environments/{id}/work/{wid}/ack`
- `/v1/environments/{id}/work/{wid}/heartbeat`
- `/v1/environments/{id}/work/{wid}/stop`
- `/v1/code/sessions/{id}/worker/{wid}`
- `/v2/sessions/{id}/events`
- `/v2/sessions/{id}/logs`
- `/v2/ccr-sessions/{id}/supabase-provision` (old only, removed with Supabase)
- `/v1/code/sessions/{id}/sign-commit`

## Confidence Assessment

| Finding                     | Confidence | Evidence                                     |
| --------------------------- | ---------- | -------------------------------------------- |
| Supabase MCP server removed | **HIGH**   | 199 → 0 string matches, all symbols gone     |
| Vercel deploy removed       | **HIGH**   | 32 → 0 string matches, all symbols gone      |
| Antspace deploy removed     | **HIGH**   | 42 → 0 string matches, all symbols gone      |
| Baku features removed       | **HIGH**   | 34 → 1 string matches, function names gone   |
| New filestore/filesystem_id | **HIGH**   | New JSON fields in struct types              |
| V0/V1 context unchanged     | **HIGH**   | Struct layout comparison exact match         |
| API endpoints unchanged     | **MEDIUM** | String comparison (garble may hide new ones) |
| Env vars still functional   | **MEDIUM** | Obfuscated by garble, cannot verify          |
