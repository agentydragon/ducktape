package config

import (
	"encoding/json"
	"fmt"
	"path/filepath"
	"strings"

	"github.com/mitchellh/mapstructure"
)

// Source is an interface representing a code source that can be loaded into a session.
// Reconstructed from: config.Source (DWARF typedef -> runtime.iface)
// Methods determined from symbol table: GetType, IsRepository, GetDirectory, Validate.
type Source interface {
	// GetType returns the source type identifier string.
	GetType() string
	// IsRepository returns true if this source represents a git repository.
	IsRepository() bool
	// GetDirectory returns the working directory for this source, resolved relative to baseDir.
	GetDirectory(baseDir string) string
	// Validate checks that the source configuration is valid.
	Validate() error
}

// BaseSource is a simple source with only a type identifier.
// It is not a repository and has no directory.
// Reconstructed from: config.BaseSource (DWARF struct, offset 0: Type string)
type BaseSource struct {
	Type string `json:"type"`
}

// GetType returns the source type.
// Reconstructed from: config.BaseSource.GetType (0x83a140)
func (b BaseSource) GetType() string {
	return b.Type
}

// IsRepository always returns false for a BaseSource.
// Reconstructed from: config.BaseSource.IsRepository (0x83a160)
func (b BaseSource) IsRepository() bool {
	return false
}

// GetDirectory always returns "" for a BaseSource.
// Reconstructed from: config.BaseSource.GetDirectory (0x83a180)
func (b BaseSource) GetDirectory(baseDir string) string {
	return ""
}

// GitInfo holds git repository metadata for a source.
// Reconstructed from: config.GitInfo (DWARF struct)
// Fields: Type (offset 0), Repo (offset 16), Ref (offset 32), URL (offset 40),
//
//	Auth (offset 48), AllowUnrestrictedGitPush (offset 56).
type GitInfo struct {
	Type                     string `json:"type"`
	Repo                     string `json:"repo"`
	Ref                      *string `json:"ref"`
	URL                      *string `json:"url"`
	Auth                     *AuthConfig `json:"auth"`
	AllowUnrestrictedGitPush bool `json:"allow_unrestricted_git_push,omitempty"`
}

// AuthConfig holds source-level authentication configuration.
// Reconstructed from binary: createSourceAuthProvider at 0xaec840 accesses
// Auth+0x00/0x08 as Type string and Auth+0x10/0x18 as Token string.
// This matches auth.AuthConfig's layout (Type string, Token string).
type AuthConfig struct {
	Type  string `json:"type"`
	Token string `json:"token"`
}

// GitRepositorySource represents a git repository source.
// Reconstructed from: config.GitRepositorySource (DWARF struct)
// Fields: BaseSource (offset 0, embedded), GitInfo (offset 16).
type GitRepositorySource struct {
	BaseSource `json:",inline"`
	GitInfo    GitInfo `json:"git_info"`
}

// GetType returns the source type from the embedded BaseSource.
// Reconstructed from: config.GitRepositorySource.GetType (0x83b620)
// This is an auto-generated wrapper that delegates to BaseSource.GetType.
func (g GitRepositorySource) GetType() string {
	return g.BaseSource.Type
}

// IsRepository always returns true for a GitRepositorySource.
// Reconstructed from: config.GitRepositorySource.IsRepository (0x83a2a0)
func (g GitRepositorySource) IsRepository() bool {
	return true
}

// Validate checks that the GitRepositorySource has the correct base type,
// a non-empty GitInfo.Type, and a non-empty GitInfo.Repo.
// Reconstructed from: config.GitRepositorySource.Validate (0x83a1a0)
// Assembly flow: checks BaseSource.Type == "git_repository" (len 0xe at SP+0x50),
// then GitInfo.Type != "" (len at SP+0x60), then GitInfo.Repo != "" (len at SP+0x70).
func (g GitRepositorySource) Validate() error {
	if g.BaseSource.Type != "git_repository" {
		return fmt.Errorf("invalid type for GitRepositorySource: %s", g.BaseSource.Type)
	}
	if g.GitInfo.Type == "" {
		return fmt.Errorf("git source type is required")
	}
	if g.GitInfo.Repo == "" {
		return fmt.Errorf("git source repo is required")
	}
	return nil
}

// GetDirectory returns the working directory derived from the git source's repository URL.
// For "github" and "github-ssh" types, it splits the repo by "/" and uses the last segment.
// For "test-file" type, it uses filepath.Base of the repo.
// The result is joined with baseDir if non-empty.
// Reconstructed from: config.GitRepositorySource.GetDirectory (0x83a2c0)
func (g GitRepositorySource) GetDirectory(baseDir string) string {
	if baseDir == "" {
		return ""
	}

	var dirName string
	switch g.GitInfo.Type {
	case "github", "github-ssh":
		parts := strings.Split(g.GitInfo.Repo, "/")
		if len(parts) > 0 {
			dirName = parts[len(parts)-1]
		}
	case "test-file":
		dirName = filepath.Base(g.GitInfo.Repo)
		if dirName == "." || dirName == "/" {
			dirName = ""
		}
	}

	if dirName == "" {
		return ""
	}

	return filepath.Join(baseDir, dirName)
}

// McpServerConfigTool represents a tool configuration within an MCP server.
// Reconstructed from: config.McpServerConfigTool (DWARF struct)
// Fields: Name (offset 0), Enabled (offset 16), PermissionPolicy (offset 24).
type McpServerConfigTool struct {
	Name             string           `mapstructure:"name,omitempty" json:"name,omitempty"`
	Enabled          *bool            `json:"enabled,omitempty"`
	PermissionPolicy PermissionPolicy `json:"permissionPolicy,omitempty"`
}

// PermissionPolicy is a string type for permission policies.
// Reconstructed from: config.PermissionPolicy (DWARF: str *uint8, len int -- string typedef)
type PermissionPolicy string

// McpServerConfig represents the configuration for an MCP server.
// Reconstructed from: config.McpServerConfig (DWARF struct)
// Fields: Type (offset 0), URL (offset 16), Command (offset 32), Args (offset 48),
//
//	Headers (offset 72), Tools (offset 80), Extra (offset 104).
type McpServerConfig struct {
	Type    string                 `mapstructure:"type,omitempty" json:"type,omitempty"`
	URL     string                 `mapstructure:"url,omitempty" json:"url,omitempty"`
	Command string                 `mapstructure:"command,omitempty" json:"command,omitempty"`
	Args    []string               `mapstructure:"args,omitempty" json:"args,omitempty"`
	Headers map[string]string      `mapstructure:"headers,omitempty" json:"headers,omitempty"`
	Tools   []McpServerConfigTool  `mapstructure:"tools,omitempty" json:"tools,omitempty"`
	Extra   map[string]interface{} `mapstructure:",remain" json:"-"`
}

// IsRemote returns true if the MCP server configuration represents a remote server.
// A server is remote if its Type is "sse", "http", or "ws", or if Type is empty but URL is non-empty.
// Reconstructed from: config.(*McpServerConfig).IsRemote (0x83a4a0)
func (c *McpServerConfig) IsRemote() bool {
	if c.Type != "" {
		switch c.Type {
		case "sse", "http", "ws":
			return true
		default:
			return false
		}
	}
	return c.URL != ""
}

// Validate checks that the MCP server configuration is consistent.
// A remote server (type/url) and a local server (command/args) cannot be mixed.
// If remote, a URL must be provided.
// Reconstructed from: config.(*McpServerConfig).Validate (0x83a500)
func (c *McpServerConfig) Validate() error {
	hasRemoteFields := c.Type != "" || c.URL != ""
	hasLocalFields := c.Command != ""

	if hasRemoteFields && hasLocalFields {
		return fmt.Errorf("MCP server config cannot have both remote fields (type/url) and local fields (command/args)")
	}

	if !hasRemoteFields && !hasLocalFields {
		return fmt.Errorf("MCP server config must specify either remote fields (type/url) or local fields (command)")
	}

	if hasRemoteFields && c.URL == "" {
		return fmt.Errorf("remote MCP server must have a URL")
	}

	return nil
}

// McpConfig represents the top-level MCP configuration containing server definitions.
// Reconstructed from: config.McpConfig (DWARF struct)
// Fields: McpServers (offset 0), Extra (offset 8).
type McpConfig struct {
	McpServers map[string]*McpServerConfig `mapstructure:"mcpServers" json:"mcpServers"`
	Extra      map[string]interface{}      `mapstructure:",remain" json:"-"`
}

// McpConfigFile represents an MCP configuration file to be written to disk.
// Reconstructed from: config.McpConfigFile (DWARF struct)
// Fields: Path (offset 0), Content (offset 16), Mode (offset 32).
type McpConfigFile struct {
	Path    string `json:"path"`
	Content string `json:"content"`
	Mode    int    `json:"mode"`
}

// OutcomeField represents an outcome configuration entry.
// Reconstructed from: config.OutcomeField (DWARF struct)
// Fields: Type (offset 0), GitInfo (offset 16).
type OutcomeField struct {
	Type    string         `json:"type"`
	GitInfo GitOutcomeInfo `json:"git_info"`
}

// GitOutcomeInfo holds git outcome metadata for push results.
// Reconstructed from: config.GitOutcomeInfo (DWARF struct)
// Fields: Type (offset 0), Repo (offset 16), Branches (offset 32).
type GitOutcomeInfo struct {
	Type     string   `json:"type"`
	Repo     string   `json:"repo"`
	Branches []string `json:"branches"`
}

// StartupContext holds the full startup configuration for a session.
// Reconstructed from: config.StartupContext (DWARF struct)
// Field offsets: Sources(0), APIBaseURL(24), Outcomes(40), CustomSystemPrompt(64),
//
//	AppendSystemPrompt(80), Model(96), McpConfig(112), AllowedTools(120),
//	DisallowedTools(144), EnabledTools(168), ClaudeCodeArgs(192), McpConfigFile(200),
//	UseSandboxGatewayConfig(208), Entrypoint(216), EnvironmentVariables(232),
//	EnvironmentSubType(240).
type StartupContext struct {
	Sources                 []Source          `json:"-"`
	SessionID               string            `json:"session_id,omitempty"`
	APIBaseURL              string            `json:"api_base_url,omitempty"`
	Outcomes                []OutcomeField    `json:"outcomes,omitempty"`
	CustomSystemPrompt      string            `json:"custom_system_prompt,omitempty"`
	AppendSystemPrompt      string            `json:"append_system_prompt,omitempty"`
	Model                   string            `json:"model,omitempty"`
	McpConfig               *McpConfig        `json:"mcp_config,omitempty"`
	AllowedTools            []string          `json:"allowed_tools,omitempty"`
	DisallowedTools         []string          `json:"disallowed_tools,omitempty"`
	EnabledTools            []string          `json:"enabled_tools,omitempty"`
	ClaudeCodeArgs          map[string]string `json:"claude_code_args,omitempty"`
	McpConfigFile           *McpConfigFile    `json:"mcp_config_file,omitempty"`
	UseSandboxGatewayConfig bool              `json:"use_sandbox_gateway_config,omitempty"`
	Entrypoint              string            `json:"entrypoint,omitempty"`
	EnvironmentVariables    map[string]string `json:"environment_variables,omitempty"`
	EnvironmentSubType      string            `json:"environment_sub_type,omitempty"`
}

// startupContextJSON is the intermediate struct used for JSON unmarshalling of StartupContext.
// It deserializes sources as raw JSON messages so they can be dispatched by type.
// Reconstructed from DWARF: the anonymous struct used in (*StartupContext).UnmarshalJSON.
type startupContextJSON struct {
	Sources                 []json.RawMessage `json:"sources"`
	APIBaseURL              string            `json:"api_base_url,omitempty"`
	Outcomes                []OutcomeField    `json:"outcomes,omitempty"`
	CustomSystemPrompt      string            `json:"custom_system_prompt,omitempty"`
	AppendSystemPrompt      string            `json:"append_system_prompt,omitempty"`
	Model                   string            `json:"model,omitempty"`
	McpConfig               map[string]interface{} `json:"mcp_config,omitempty"`
	AllowedTools            []string          `json:"allowed_tools,omitempty"`
	DisallowedTools         []string          `json:"disallowed_tools,omitempty"`
	EnabledTools            []string          `json:"enabled_tools,omitempty"`
	ClaudeCodeArgs          map[string]string `json:"claude_code_args,omitempty"`
	McpConfigFile           *McpConfigFile    `json:"mcp_config_file,omitempty"`
	UseSandboxGatewayConfig bool              `json:"use_sandbox_gateway_config,omitempty"`
	Entrypoint              string            `json:"entrypoint,omitempty"`
	EnvironmentVariables    map[string]string `json:"environment_variables,omitempty"`
	EnvironmentSubType      string            `json:"environment_sub_type,omitempty"`
}

// sourceTypeJSON is used to peek at a source's type field before full deserialization.
// Reconstructed from: the BaseSource-like struct unmarshalled in UnmarshalJSON's source loop.
type sourceTypeJSON struct {
	Type string `json:"type"`
}

// UnmarshalJSON implements custom JSON unmarshalling for StartupContext.
// It first unmarshals into an intermediate struct, then dispatches each source
// by its "type" field: "git_repository" sources are decoded as GitRepositorySource,
// while other types produce an error.
// If mcp_config is present, it is decoded via mapstructure into *McpConfig.
// Reconstructed from: config.(*StartupContext).UnmarshalJSON (0x83a5e0)
func (sc *StartupContext) UnmarshalJSON(data []byte) error {
	var raw startupContextJSON
	if err := json.Unmarshal(data, &raw); err != nil {
		return fmt.Errorf("failed to unmarshal startup context: %w", err)
	}

	// Copy scalar/slice fields from intermediate struct to StartupContext.
	sc.APIBaseURL = raw.APIBaseURL
	sc.Outcomes = raw.Outcomes
	sc.CustomSystemPrompt = raw.CustomSystemPrompt
	sc.AppendSystemPrompt = raw.AppendSystemPrompt
	sc.Model = raw.Model
	sc.AllowedTools = raw.AllowedTools
	sc.DisallowedTools = raw.DisallowedTools
	sc.EnabledTools = raw.EnabledTools
	sc.ClaudeCodeArgs = raw.ClaudeCodeArgs
	sc.McpConfigFile = raw.McpConfigFile
	sc.UseSandboxGatewayConfig = raw.UseSandboxGatewayConfig
	sc.Entrypoint = raw.Entrypoint
	sc.EnvironmentVariables = raw.EnvironmentVariables
	sc.EnvironmentSubType = raw.EnvironmentSubType

	// Decode mcp_config via mapstructure if present.
	if raw.McpConfig != nil {
		mcpConfig := new(McpConfig)
		if err := mapstructure.Decode(raw.McpConfig, mcpConfig); err != nil {
			return fmt.Errorf("failed to decode MCP config: %w", err)
		}
		sc.McpConfig = mcpConfig
	}

	// Parse each source by type.
	sc.Sources = make([]Source, 0, len(raw.Sources))
	for i, rawSource := range raw.Sources {
		var st sourceTypeJSON
		if err := json.Unmarshal(rawSource, &st); err != nil {
			return fmt.Errorf("failed to parse source[%d] type: %w", i, err)
		}

		switch st.Type {
		case "git_repository":
			var grs GitRepositorySource
			if err := json.Unmarshal(rawSource, &grs); err != nil {
				return fmt.Errorf("failed to parse git repository source[%d]: %w", i, err)
			}
			sc.Sources = append(sc.Sources, grs)
		default:
			return fmt.Errorf("unknown source type[%d]: %s", i, st.Type)
		}
	}

	return nil
}

// Validate checks that the StartupContext has a non-empty APIBaseURL and that
// all sources are non-nil and individually valid.
// Reconstructed from: config.(*StartupContext).Validate (0x83ae80)
func (sc *StartupContext) Validate() error {
	if sc.APIBaseURL == "" {
		return fmt.Errorf("api_base_url is required in startup_context")
	}
	for i, source := range sc.Sources {
		if source == nil {
			return fmt.Errorf("source[%d] is nil", i)
		}
		if err := source.Validate(); err != nil {
			return fmt.Errorf("source[%d] validation failed: %w", i, err)
		}
	}
	return nil
}

// Len returns the total number of fields/elements in the startup context
// (approximate measure of context size for logging).
func (sc *StartupContext) Len() int {
	return len(sc.Sources) + len(sc.Outcomes) + len(sc.AllowedTools) + len(sc.DisallowedTools) + len(sc.EnabledTools)
}

// NumSources returns the number of sources in the startup context.
func (sc *StartupContext) NumSources() int {
	return len(sc.Sources)
}

// NumLanguages returns the number of enabled tools (used as a proxy for language count in logging).
func (sc *StartupContext) NumLanguages() int {
	return len(sc.EnabledTools)
}

// EnvironmentConfig represents the environment configuration from input.
// Reconstructed from callers in v0_parser.go and v1_parser.go.
type EnvironmentConfig struct {
	EnvironmentType string                 `json:"environment_type"`
	Cwd             string                 `json:"cwd,omitempty"`
	InitScript      string                 `json:"init_script,omitempty"`
	McpServers      map[string]interface{} `json:"mcp_servers,omitempty"`
}

// Session represents a session configuration with its ID and startup context.
// Reconstructed from: config.Session (DWARF struct)
// Fields: SessionID (offset 0), WorkID (offset 16), StartupContext (offset 32).
type Session struct {
	SessionID      string          `json:"session_id"`
	WorkID         string          `json:"work_id"`
	StartupContext *StartupContext `json:"startup_context"`
}

// Validate checks that the Session has a non-empty SessionID and a valid StartupContext.
// Reconstructed from: config.(*Session).Validate (0x83b000)
func (s *Session) Validate() error {
	if s.SessionID == "" {
		return fmt.Errorf("session id is required")
	}
	if s.StartupContext == nil {
		return fmt.Errorf("startup context is required")
	}
	if err := s.StartupContext.Validate(); err != nil {
		return fmt.Errorf("invalid startup context: %w", err)
	}
	return nil
}
