// Reconstructed from binary: Build ID 0b86a2a0 (release-1186d93b9-ext)
// Source: internal/api/session_context_types.go (session/task wire contract)
//
// Every struct below was recovered field-by-field from the binary's Go runtime
// type metadata (`abi.StructType`), not guessed: `.typelink` was walked from
// `moduledata.types` (0x25e3020, moduledata at VMA 0x3b9a5c0) and each struct's
// field names, Go types, tags and byte offsets read out of `abi.StructField`.
// The `// vaddr` comment on each type is the address of its `abi.Type`.
//
// Garble randomises package identifiers per build; the map below is for this
// build only (see ../../degarble_map.md):
//
//	vQQ7qdSzWbmR / WWD9Ee6Wrf4m -> internal/api        (backend client)
//	biAUDaIjL    / JKzN_Mds     -> session/source config types
//	phazNUhJlTW  / zMACuH       -> auth
//	ejg54mRv_    / Ciyypbbc_    -> encoding/json  (YgfBAro0f = json.RawMessage,
//	                               confirmed kind=slice size=0x18)
//
// The exported Go names here are invented for readability; the *json tags and
// offsets* are the binary's. Where a name is a guess it says so.

package api

import "encoding/json"

// ---------------------------------------------------------------------------
// Session context (v1 work response)
// ---------------------------------------------------------------------------

// SessionWorkContext is the JSON body the environment manager receives for a
// unit of work (the v1 `/v1/environments/.../work` response payload). It is the
// top-level session configuration handed to `environment-manager` before it
// launches Claude Code.
//
// Binary: vQQ7qdSzWbmR.E5N7JLT, vaddr 0x28eabe0, size 0x108, 16 fields.
//
// Predecessor in the previous binary (release-d84d76b7-ext):
// ox0nuS.FTzYSQ5OY, vaddr 0x246cd40, size 0xa8, 12 fields.
//
// Delta vs. the previous binary:
//   - REMOVED `use_code_sessions` (was bool at +0x78)
//   - ADDED   `git_mount_base_url`, `agent_files`, `main_agent`,
//     `auto_mode_allow`, `launcher_hooks`
//
// `Sources` carries `json:"-"`: the type has a custom UnmarshalJSON that decodes
// into the shadow struct below and then dispatches each raw source on its
// `"type"` discriminator (see sourceTypeProbe / GitSourceEnvelope).
type SessionWorkContext struct {
	Version                 int               `json:"version"`                              // +0x00
	SessionIngressToken     string            `json:"session_ingress_token"`                // +0x08
	APIBaseURL              string            `json:"api_base_url"`                         // +0x18
	Sources                 []Source          `json:"-"`                                    // +0x28
	Auth                    []AuthEntry       `json:"auth"`                                 // +0x40
	ClaudeCodeArgs          map[string]string `json:"claude_code_args,omitempty"`           // +0x58
	MCPConfig               *MCPConfigFile    `json:"mcp_config,omitempty"`                 // +0x60
	UseSandboxGatewayConfig bool              `json:"use_sandbox_gateway_config,omitempty"` // +0x68
	EnvironmentVariables    map[string]string `json:"environment_variables,omitempty"`      // +0x70
	Outcomes                json.RawMessage   `json:"outcomes,omitempty"`                   // +0x78
	Cwd                     string            `json:"cwd,omitempty"`                        // +0x90
	GitMountBaseURL         string            `json:"git_mount_base_url,omitempty"`         // +0xa0
	AgentFiles              []AgentFile       `json:"agent_files,omitempty"`                // +0xb0
	MainAgent               string            `json:"main_agent,omitempty"`                 // +0xc8
	AutoModeAllow           []string          `json:"auto_mode_allow,omitempty"`            // +0xd8
	LauncherHooks           []LauncherHook    `json:"launcher_hooks,omitempty"`             // +0xf0
}

// sessionWorkContextWire is the decode-time shadow of SessionWorkContext: byte
// -for-byte the same layout, but `sources` is left as raw JSON so the custom
// unmarshaller can dispatch each element on its `"type"` field.
//
// Binary: anonymous struct type, vaddr 0x28e8c00, size 0x108, 16 fields.
// (Previous binary: anonymous struct at vaddr 0x246ac40, size 0xa8.)
type sessionWorkContextWire struct {
	Version                 int               `json:"version"`                              // +0x00
	SessionIngressToken     string            `json:"session_ingress_token"`                // +0x08
	APIBaseURL              string            `json:"api_base_url"`                         // +0x18
	Sources                 []json.RawMessage `json:"sources"`                              // +0x28
	Auth                    []AuthEntry       `json:"auth"`                                 // +0x40
	ClaudeCodeArgs          map[string]string `json:"claude_code_args,omitempty"`           // +0x58
	MCPConfig               *MCPConfigFile    `json:"mcp_config,omitempty"`                 // +0x60
	UseSandboxGatewayConfig bool              `json:"use_sandbox_gateway_config,omitempty"` // +0x68
	EnvironmentVariables    map[string]string `json:"environment_variables,omitempty"`      // +0x70
	Outcomes                json.RawMessage   `json:"outcomes,omitempty"`                   // +0x78
	Cwd                     string            `json:"cwd,omitempty"`                        // +0x90
	GitMountBaseURL         string            `json:"git_mount_base_url,omitempty"`         // +0xa0
	AgentFiles              []AgentFile       `json:"agent_files,omitempty"`                // +0xb0
	MainAgent               string            `json:"main_agent,omitempty"`                 // +0xc8
	AutoModeAllow           []string          `json:"auto_mode_allow,omitempty"`            // +0xd8
	LauncherHooks           []LauncherHook    `json:"launcher_hooks,omitempty"`             // +0xf0
}

// TODO(re): SessionWorkContext.UnmarshalJSON body not disassembled. It must
// decode into sessionWorkContextWire and then decode each element of
// wire.Sources via sourceTypeProbe; the dispatch table is not yet recovered.

// ---------------------------------------------------------------------------
// task-run input
// ---------------------------------------------------------------------------

// TaskRunInput is the JSON document accepted by the `task-run` subcommand (and
// by the orchestrator when it hands a task to a runner). It is a superset of
// SessionWorkContext's Claude-Code-shaping fields.
//
// Binary: biAUDaIjL.T3aXraXmb5Zl, vaddr 0x28f6920, size 0x188, 23 fields.
// Reachable from vQQ7qdSzWbmR.VDQQbZD (+0x00) and fkWASaBCp.EAT9SthH (+0x30),
// i.e. it is what both the API path and the Claude Code launcher read.
//
// Predecessor: j5dyUe.TUdhAnKiu, vaddr 0x247d240, size 0x108, 17 fields.
//
// Delta vs. the previous binary:
//
//	REMOVED  `mcp_config` (*j5dyUe.JRJuj5 — the *typed* MCP config, a
//	         map[string]*MCPServerConfig; deleting it is what removed the
//	         `command`, `args`, `enabled` and `permission_policy` json tags
//	         from the whole binary). Only `mcp_config_file` survives.
//	REMOVED  `use_code_sessions` (bool)
//	ADDED    `kindling_seed_ccsr_token`, `git_via_egress_gateway`,
//	         `git_mount_base_url`, `baku_backend_provider`, `agent_files`,
//	         `main_agent`, `auto_mode_allow`, `launcher_hooks`
type TaskRunInput struct {
	Sources                 []Source          `json:"sources"`                              // +0x000
	APIBaseURL              string            `json:"api_base_url,omitempty"`               // +0x018
	Outcomes                []Outcome         `json:"outcomes,omitempty"`                   // +0x028
	CustomSystemPrompt      string            `json:"custom_system_prompt,omitempty"`       // +0x040
	AppendSystemPrompt      string            `json:"append_system_prompt,omitempty"`       // +0x050
	Model                   string            `json:"model,omitempty"`                      // +0x060
	AllowedTools            []string          `json:"allowed_tools,omitempty"`              // +0x070
	DisallowedTools         []string          `json:"disallowed_tools,omitempty"`           // +0x088
	EnabledTools            []string          `json:"enabled_tools,omitempty"`              // +0x0a0
	ClaudeCodeArgs          map[string]string `json:"claude_code_args,omitempty"`           // +0x0b8
	MCPConfigFile           *MCPConfigFile    `json:"mcp_config_file,omitempty"`            // +0x0c0
	UseSandboxGatewayConfig bool              `json:"use_sandbox_gateway_config,omitempty"` // +0x0c8
	Entrypoint              string            `json:"entrypoint,omitempty"`                 // +0x0d0
	EnvironmentVariables    map[string]string `json:"environment_variables,omitempty"`      // +0x0e0
	KindlingSeedCCSRToken   string            `json:"kindling_seed_ccsr_token,omitempty"`   // +0x0e8
	GitViaEgressGateway     bool              `json:"git_via_egress_gateway,omitempty"`     // +0x0f8
	GitMountBaseURL         string            `json:"git_mount_base_url,omitempty"`         // +0x100
	EnvironmentSubType      string            `json:"environment_sub_type,omitempty"`       // +0x110
	BakuBackendProvider     string            `json:"baku_backend_provider,omitempty"`      // +0x120
	AgentFiles              []AgentFile       `json:"agent_files,omitempty"`                // +0x130
	MainAgent               string            `json:"main_agent,omitempty"`                 // +0x148
	AutoModeAllow           []string          `json:"auto_mode_allow,omitempty"`            // +0x158
	LauncherHooks           []LauncherHook    `json:"launcher_hooks,omitempty"`             // +0x170
}

// ---------------------------------------------------------------------------
// Sources
// ---------------------------------------------------------------------------

// Source is the interface every concrete source descriptor satisfies.
//
// Binary: biAUDaIjL.EPx6Y2 — an interface type (abi kind 20). Method set not
// yet enumerated.
//
// TODO(re): enumerate Source's methods from its abi.InterfaceType and map the
// itabs so the full set of concrete implementors is known. `kItfsbt_` (the
// source/repo descriptor package) exposes GetDirectory, GetType, IsRepository,
// IsHermeticMode, UsesWorkspaceRootCwd, Validate — those are the likely methods.
type Source interface{}

// sourceTypeProbe is the discriminator-only struct used to peek at a raw source
// element's `"type"` before decoding it into a concrete type.
//
// Binary: biAUDaIjL.DPgbZTPcz_P, vaddr 0x27e7960, size 0x10, 1 field.
// (Previous binary: j5dyUe.ALzz2E, vaddr 0x2397020.)
type sourceTypeProbe struct {
	Type string `json:"type"` // +0x00
}

// GitSourceEnvelope is a git source as it appears inside `sources`: the
// discriminator is embedded, the payload hangs off `git_info`.
//
// Binary: biAUDaIjL.FCP9HKL, vaddr 0x2897ae0, size 0x78, 2 fields.
// (Previous binary: j5dyUe.MWqZHi, vaddr 0x242f540, size 0x68.)
type GitSourceEnvelope struct {
	sourceTypeProbe           // +0x00 (embedded)
	GitInfo         GitSource `json:"git_info"` // +0x10
}

// GitSource describes one git repository to materialise into the environment.
//
// Binary: biAUDaIjL.Fqynvh, vaddr 0x28c3d40, size 0x68, 8 fields.
// Predecessor: j5dyUe.S94HOGaYA, vaddr 0x2457a20, size 0x58, 8 fields.
//
// Delta vs. the previous binary:
//
//	REMOVED `allow_unrestricted_git_push` (bool, was at +0x48)
//	ADDED   `sparse_checkout_paths` ([]string, +0x50)
//
// `sparse_checkout_paths` is a per-repository list; the consumer is the git
// source processor (garbled package `JKzN_Mds`, methods ProcessSources /
// Process / CanHandle).
//
// TODO(re): the code that consumes SparseCheckoutPaths has NOT been read. It
// almost certainly drives `git sparse-checkout set`, but that is inference from
// the field name and its position next to MountPath — not from disassembly.
type GitSource struct {
	Type                string     `json:"type"`                            // +0x00
	Repo                string     `json:"repo"`                            // +0x10
	Ref                 *string    `json:"ref,omitempty"`                   // +0x20
	URL                 *string    `json:"url,omitempty"`                   // +0x28
	Host                string     `json:"host,omitempty"`                  // +0x30
	Auth                *AuthEntry `json:"auth,omitempty"`                  // +0x40
	MountPath           *string    `json:"mount_path,omitempty"`            // +0x48
	SparseCheckoutPaths []string   `json:"sparse_checkout_paths,omitempty"` // +0x50
}

// ---------------------------------------------------------------------------
// Outcomes
// ---------------------------------------------------------------------------

// Outcome describes where the session's result should be delivered.
//
// Binary: biAUDaIjL.C4keWHDJvA, vaddr 0x27c20a0, size 0x48, 2 fields.
// (Previous binary: j5dyUe.J59a3Sb, vaddr 0x2375740 — same shape.)
type Outcome struct {
	Type    string        `json:"type"`     // +0x00
	GitInfo OutcomeGitRef `json:"git_info"` // +0x10
}

// OutcomeGitRef is the git target of an outcome.
//
// Binary: biAUDaIjL.LogGCEm, vaddr 0x27e7ae0, size 0x38, 3 fields.
// (Previous binary: j5dyUe.ZAtEU2mt, vaddr 0x23971a0 — same shape.)
type OutcomeGitRef struct {
	Type     string   `json:"type"`     // +0x00
	Repo     string   `json:"repo"`     // +0x10
	Branches []string `json:"branches"` // +0x20
}

// ---------------------------------------------------------------------------
// Leaf payload types
// ---------------------------------------------------------------------------

// AuthEntry is one credential handed to the environment.
//
// Binary: phazNUhJlTW.OZyKGMA, vaddr 0x27c2000, size 0x20, 2 fields.
// (Previous binary: f5mf2ac1HD.Idm3uKm0aZiN, vaddr 0x2375600 — unchanged.)
type AuthEntry struct {
	Type  string `json:"type"`  // +0x00
	Token string `json:"token"` // +0x10
}

// MCPConfigFile is a file to materialise on disk (used for `mcp_config` and
// `mcp_config_file`).
//
// Binary: biAUDaIjL.Aag1na, vaddr 0x27e7ba0, size 0x28, 3 fields.
// (Previous binary: j5dyUe.YKG8nk, vaddr 0x2397260 — unchanged.)
//
// Note: the previous binary ALSO carried a typed MCP config
// (j5dyUe.JRJuj5 = `{mcpServers: map[string]*MCPServerConfig}` where
// MCPServerConfig = j5dyUe.NXInJileqHmm {type,url,command,args,headers,tools}
// and tools = j5dyUe.IaWu9wXqmZNi {name,enabled,permission_policy}).
// That whole tree is GONE in this build; the only surviving typed view of an
// MCP config file is the untyped `struct{ McpServers map[string]json.RawMessage
// "json:\"mcpServers\"" }` at vaddr 0x276cfa0. That single deletion accounts
// for the removed `command`, `args`, `enabled` and `permission_policy` tags.
type MCPConfigFile struct {
	Path    string `json:"path"`    // +0x00
	Content string `json:"content"` // +0x10
	Mode    int    `json:"mode"`    // +0x20
}

// AgentFile is a named agent definition injected into the environment
// (`.claude/agents/<name>` in Claude Code terms).
//
// Binary: biAUDaIjL.Xf1_gSfD3, vaddr 0x27c2140, size 0x20, 2 fields. NEW in
// this build (no counterpart in the previous binary).
//
// Consumed together with TaskRunInput.MainAgent, which names which of these
// agents is the top-level one.
type AgentFile struct {
	Name    string `json:"name"`    // +0x00
	Content string `json:"content"` // +0x10
}

// LauncherHook is a hook script the launcher installs before starting Claude
// Code: `Script` is written to `Filename` and registered for `Event`.
//
// Binary: biAUDaIjL.BaAH4I, vaddr 0x27e7c60, size 0x30, 3 fields. NEW in this
// build. The only surviving exported reader is nwTZT_5.(*MAKP4pBZG).GetHooks
// at 0x16db300.
//
// TODO(re): GetHooks is not disassembled, so the accepted `event` values (and
// whether Filename is a path or a basename under the hooks dir) are unknown.
type LauncherHook struct {
	Event    string `json:"event"`    // +0x00
	Filename string `json:"filename"` // +0x10
	Script   string `json:"script"`   // +0x20
}

// ---------------------------------------------------------------------------
// Environment / work records
// ---------------------------------------------------------------------------

// WorkRecord is an environment work item as returned by the environments API.
//
// Binary: vQQ7qdSzWbmR.JNZ8DRQBS, vaddr 0x28bb680, size 0x80, 7 fields.
// (Previous binary: ox0nuS.QOoiU9mmIg, vaddr 0x244fd40 — unchanged.)
type WorkRecord struct {
	ID            string   `json:"id"`             // +0x00
	Type          string   `json:"type"`           // +0x10
	EnvironmentID string   `json:"environment_id"` // +0x20
	State         string   `json:"state"`          // +0x30
	Data          WorkData `json:"data"`           // +0x40
	Secret        string   `json:"secret"`         // +0x60
	CreatedAt     string   `json:"created_at"`     // +0x70
}

// WorkData is the discriminated payload of a WorkRecord.
//
// Binary: vQQ7qdSzWbmR.VeCSVajYY, vaddr 0x27c2320, size 0x20, 2 fields.
// (Previous binary: ox0nuS.DL6aqg, vaddr 0x2375880 — unchanged.)
type WorkData struct {
	Type string `json:"type"` // +0x00
	ID   string `json:"id"`   // +0x10
}

// StartupContextEnvelope is the three-part blob the environment manager is
// handed at start-up; each part is kept raw and decoded by a different owner
// (`startup_context` -> session init, `environment` -> SessionWorkContext,
// `auth` -> []AuthEntry).
//
// Binary: vQQ7qdSzWbmR.l8zZ0H, vaddr 0x27e7f60, size 0x48, 3 fields.
// (Previous binary: ox0nuS.zk0AvnOETjg, vaddr 0x2397560 — unchanged.)
type StartupContextEnvelope struct {
	StartupContext json.RawMessage `json:"startup_context"` // +0x00
	Environment    json.RawMessage `json:"environment"`     // +0x18
	Auth           json.RawMessage `json:"auth"`            // +0x30
}

// ---------------------------------------------------------------------------
// What the new fields control
// ---------------------------------------------------------------------------
//
// GitMountBaseURL — DETERMINED (partially).
//   `JKzN_Mds.(*HqAmVlK5).SetGitMountBaseURL` at 0x22a3880 is NEW in this build
//   (no such method in the previous binary). It is a plain setter: it stores the
//   string into the git-source processor at +0x98/+0xa0. The processor is the
//   same object that owns SetupGitProxyAfterSourcesProcessed / UpdateRemoteURLs
//   / RemoveStaleLoopbackGitConfig, and the container environment carries a
//   matching NEW env var `CCR_GITPROXY_BASE_URL` (present in the current
//   binary's runtime string set, absent from the previous one). So: it overrides
//   the base URL that git remotes are rewritten to / that the in-container git
//   proxy is advertised as.
//   TODO(re): the read side (which callers of the +0x98 field build URLs from
//   it) is not disassembled, and the CCR_GITPROXY_BASE_URL link is corroborative,
//   not proven.
//
// KindlingSeedCCSRToken — DETERMINED (by name-identical env var).
//   The runtime string set of THIS binary contains `CCR_KINDLING_SEED_CCSR_TOKEN`
//   and the previous binary's does not; the json tag `kindling_seed_ccsr_token`
//   appears in the same build. The 1:1 name correspondence plus the fact that
//   every other CCR_* variable in that table is an env var handed to the Claude
//   process makes this a seed token forwarded into Claude Code's environment.
//   TODO(re): the exact writer (GetClaudeEnvironmentVariables at 0x2399e40 /
//   0x23f2900) was not disassembled; the string itself is garble-encrypted.
//
// GitViaEgressGateway — NOT DETERMINED.
//   The env var `CCR_EGRESS_GATEWAY_ENABLED` exists in BOTH binaries, so its
//   presence is not evidence that this new bool drives it. No consumer of the
//   bool at TaskRunInput+0xf8 was located.
//
// BakuBackendProvider — NOT DETERMINED.
//   The only `baku` occurrences in the binary are the tzdata entries for
//   Asia/Baku. Every candidate value is a garble-encrypted literal, and no
//   consumer was located.
//
// SparseCheckoutPaths — NOT DETERMINED (consumer not read); see GitSource.
//
// Note on tags that are NOT part of this contract: `skills`, `mcp_servers`,
// `theme`, `claude_code_version`, `env`, `argv`, `src`, `defer_loading`,
// `action`, `form`, `cancel`, `request`/`requests` all appear in this build but
// belong to other packages — `skills`/`mcp_servers`/`claude_code_version` to the
// Claude Code stream-json envelope (see ../o11y/startup_timing.go), and
// `theme`/`src`/`defer_loading`/`action`/`form`/`cancel`/`requests` to the MCP
// SDK types in garbled package `eLfXMalY3X`. None of them is a session-context
// field.
