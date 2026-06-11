# Binary Diff Results: 64bc4dc1 vs 495ea204

Comparison of old (`64bc4dc1`, 49.8 MB, `release-9f4ec76fbc-ext`) and new
(`495ea204`, 51.9 MB, `release-d84d76b7-ext`) environment-manager binaries.
Both are garble-obfuscated, stripped ELF x86-64.

## Method

Full string extraction from both binaries (`strings -n 6`), followed by:
JSON field tag comparison, struct layout extraction from Go type reflection,
CamelCase identifier extraction from garble concatenation blobs, targeted
keyword searches, and `--help` output comparison.

## Summary

The 495ea204 binary is an incremental update over 64bc4dc1. The changes are
modest: one JSON field added, two fields removed from the V1 StartupContext
struct, a new `RecordLongRunningStep` instrumentation feature, and several
minor identifier additions/removals. No CLI flag changes, no API endpoint
changes, no new commands.

## CLI Help Output: UNCHANGED

All four commands (`--help`, `task-run --help`, `orchestrator --help`,
`setup --help`) produce **identical output** between old and new binaries.
No new flags, no removed flags, no changed defaults.

The binary name in help is still `environment-runner`.

## V1 StartupContext: `custom_system_prompt` and `append_system_prompt` REMOVED

**This is the most significant change.** The V1 session context struct lost
two fields that were present in the old binary.

| Field                  | Old (64bc4dc1) | New (495ea204) |
| ---------------------- | -------------- | -------------- |
| `custom_system_prompt` | present        | **REMOVED**    |
| `append_system_prompt` | present        | **REMOVED**    |

Old V1 struct fields (in order):

```
version, session_ingress_token, api_base_url, sources, auth,
claude_code_args, mcp_config, use_sandbox_gateway_config,
environment_variables, use_code_sessions, outcomes,
custom_system_prompt, append_system_prompt, cwd
```

New V1 struct fields (in order):

```
version, session_ingress_token, api_base_url, sources, auth,
claude_code_args, mcp_config, use_sandbox_gateway_config,
environment_variables, use_code_sessions, outcomes, cwd
```

**Note:** The V0 Session struct still has both `custom_system_prompt` and
`append_system_prompt`. These fields were only removed from V1.

**Impact on RE:** The V1 input parser no longer reads system prompt overrides
from the startup context. System prompts may now be managed differently in V1
mode (perhaps via the API or MCP config instead of the startup context JSON).

## V0 Session Context: UNCHANGED

Both binaries have identical V0 fields:

```
sources, api_base_url, outcomes, custom_system_prompt,
append_system_prompt, model, mcp_config, allowed_tools,
disallowed_tools, enabled_tools, claude_code_args,
mcp_config_file, use_sandbox_gateway_config, entrypoint,
environment_variables, environment_sub_type, use_code_sessions
```

## Heartbeat Response: UNCHANGED

Both binaries have identical heartbeat fields:

```
lease_extended, state, last_heartbeat, ttl_seconds, lease_updated_at
```

## New JSON Field Tags

One new JSON field tag appears in the new binary:

| Field        | Context                                                    |
| ------------ | ---------------------------------------------------------- |
| `mount_path` | New field (appears alongside `WriteOnly` in struct layout) |

The `filestore_url`, `filesystem_id`, and `jwt` fields from the previous
64bc4dc1 diff remain present in both binaries (they were already in 64bc4dc1).

## New Go Identifiers (from garble concatenation blobs)

### Added in 495ea204

| Identifier                     | Likely meaning                                           |
| ------------------------------ | -------------------------------------------------------- |
| `RecordLongRunningStep`        | New instrumentation (46 references)                      |
| `RecordStep`                   | New instrumentation (3 references)                       |
| `MountPath`                    | New struct field for mount path configuration            |
| `WriteOnlyMountPath`           | Mount path with write-only semantics                     |
| `AllowWriteThisUpdate`         | New permission/policy field                              |
| `ContentInitScript`            | Init script content field                                |
| `ContinueAllowGitConfig`       | Git config permission continuation field                 |
| `ContinueCallToolRequest`      | Tool request continuation field                          |
| `CallOptionBaseSource`         | Call option with base source                             |
| `StdinContext`                 | Stdin context field (replaces or supplements StdinInput) |
| `FilesystemID`                 | Filesystem identifier (present in blob)                  |
| `StringValuePermissionDenials` | Permission denials as string value type                  |
| `ExpandedFamilies`             | OTel metric families expansion                           |
| `ObservableForceAttemptHTTP`   | HTTP/2 force attempt observable metric                   |

### Removed from 495ea204

| Identifier         | Was in 64bc4dc1                               |
| ------------------ | --------------------------------------------- |
| `SanitizeOutput`   | Output sanitization (8 refs in old, 0 in new) |
| `PriorityWorkData` | Priority work data field                      |

## Instrumentation Changes

The `RecordLongRunningStep` feature is the largest code addition by reference
count. The old binary had 51 references to `RecordLongRunning*` patterns; the
new has 86. The `LongRunningStep` string appears 46 times in the new binary
vs 0 in the old. This suggests a new step-level instrumentation system for
tracking long-running operations during session execution.

## Feature Removals

### `SanitizeOutput`: REMOVED

`SanitizeOutput` had 8 string references in the old binary and 0 in the new.
This was likely an output sanitization feature that has been removed or
replaced with a different mechanism.

### `PriorityWorkData`: REMOVED

`PriorityWorkData` had 1 reference in the old binary and 0 in the new. This
may indicate a change in how work priority is represented.

## Binary Size

| Metric    | Old (64bc4dc1) | New (495ea204) | Delta   |
| --------- | -------------- | -------------- | ------- |
| File size | 49,818,896     | 51,859,504     | +2.0 MB |
| Strings   | 197,335        | 200,596        | +3,261  |

## API Endpoint Changes

No changes detected. API endpoint paths are fully garble-obfuscated in both
binaries (they appear in garbled runtime strings, not as plain paths). The
`--help` output confirms the same endpoints: `/v1/environments/whoami`,
`work/poll`, `work/{wid}/ack`, `work/{wid}/heartbeat`, `work/{wid}/stop`.

## File Paths: UNCHANGED

Both binaries embed the same file paths:

- `.claude/hooks`, `.claude/hooks/session-start.sh`, `.claude/settings.json`
- `.claude/stop-hook-git-check.sh`
- `/opt/node{20,21,22}/bin/node`, `/usr/local/go{1.23.5,1.24.0}`
- `/usr/local/bin/{go,gofmt,node,npm,npx,python,python3}`

## Go Runtime: UNCHANGED

Both binaries reference Go 1.23.5 and Go 1.24.0 (for runtime symlinking).

## Dependency Changes

No visible Go module dependency additions or removals. OTel semantic
convention attributes are unchanged between versions.

## Sandbox Configuration: UNCHANGED

The `enableWeakerNestedSandbox` field remains in both binaries. The
`use_sandbox_gateway_config` field is present in both V0 and V1 structs
in both versions.

## npm Package References: UNCHANGED

Both binaries embed the same `$schema` reference to
`https://json.schemastore.org/claude-code-settings.json`.

## Confidence Assessment

| Finding                                         | Confidence | Evidence                                         |
| ----------------------------------------------- | ---------- | ------------------------------------------------ |
| V1 `custom_system_prompt` removed               | **HIGH**   | Full struct layout comparison, field-by-field    |
| V1 `append_system_prompt` removed               | **HIGH**   | Full struct layout comparison, field-by-field    |
| V0 Session unchanged                            | **HIGH**   | Full struct layout comparison, field-by-field    |
| New `mount_path` JSON field                     | **HIGH**   | JSON tag extraction, 354 vs 353 total tags       |
| `RecordLongRunningStep` instrumentation added   | **HIGH**   | 0 vs 46 string matches                           |
| `SanitizeOutput` removed                        | **HIGH**   | 8 vs 0 string matches                            |
| `PriorityWorkData` removed                      | **HIGH**   | 1 vs 0 string matches                            |
| CLI flags unchanged                             | **HIGH**   | Identical `--help` output on all 4 commands      |
| Heartbeat unchanged                             | **HIGH**   | Full struct layout comparison                    |
| API endpoints unchanged                         | **MEDIUM** | Garble hides paths; inferred from help text      |
| `AllowWriteThisUpdate`, `ContentInitScript` new | **MEDIUM** | CamelCase blob extraction (garble noise present) |
