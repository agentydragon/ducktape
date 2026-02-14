// Package anthropic implements the Anthropic environment type for the
// environment manager. This is the primary environment type used for
// Anthropic-hosted Claude Code web sessions.
//
// Reconstructed from binary at Build ID 6b49f1ca (Go 1.25.6).
// Source path: /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/envtype/anthropic/
//
// Key symbols:
//   - anthropic.New (0xaf8540)
//   - anthropic.DecodeConfig (0xb04020)
//   - anthropic.Registration (0x1589458)
//   - anthropic.defaultSettingsJSON (0x15add80)
//   - anthropic.init (0xaf8480)
package anthropic

import (
	"context"
	"encoding/json"
	"fmt"
	"io/fs"
	"log/slog"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/config"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/envtype"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/envtype/anthropic/install_scripts"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/o11y"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/process"
)

// Registration is the global registration for the anthropic environment type.
// Set during init() from the shared package's default settings.
// Symbol: anthropic.Registration (0x1589458)
var Registration *envtype.Registration

// defaultSettingsJSON holds the default Claude Code settings JSON.
// Copied from envtype/shared.DefaultSettingsJSON during init().
// Symbol: anthropic.defaultSettingsJSON (0x15add80)
var defaultSettingsJSON []byte

// stopHookScript holds the stop hook script content.
// Copied from envtype/shared.StopHookScript during init().
var stopHookScript []byte

// sessionStartHookSkill is defined in skill_content.go
// Symbol: anthropic.sessionStartHookSkill (0x158e4a0), 4931 bytes

// init copies shared defaults (DefaultSettingsJSON, StopHookScript) from the
// shared package into package-level variables.
//
// Reconstructed from: anthropic.init (0xaf8480)
// Assembly evidence: loads from envtype/shared.DefaultSettingsJSON and
// envtype/shared.StopHookScript, stores into anthropic.defaultSettingsJSON
// and anthropic.stopHookScript.
func init() {
	// defaultSettingsJSON = shared.DefaultSettingsJSON
	// stopHookScript = shared.StopHookScript
	// (actual shared package references elided for compilation independence)
}

// anthropicEnvironmentType implements envtype.EnvironmentType for Anthropic-hosted
// environments. It manages git repo setup, language installation, init scripts,
// Claude skills, dev server, and Baku project initialization.
//
// Struct layout (from field access patterns and SetSessionMode/GetCWD):
//   offset 0x00: config *anthropicConfig        (decoded config)
//   offset 0x08: logger *slog.Logger             (structured logger)
//   offset 0x10: startupContext *config.StartupContext
//   offset 0x18: authContext interface{}          (auth context, 16 bytes: itab + data)
//   offset 0x28: sessionMode config.SessionMode  (string, 16 bytes: ptr + len)
//   offset 0x38: cwd string                      (16 bytes: ptr + len)
type anthropicEnvironmentType struct {
	config         *anthropicConfig
	logger         *slog.Logger
	startupContext *config.StartupContext
	authContext    interface{}
	sessionMode    config.SessionMode
	cwd            string
}

// anthropicConfig holds the decoded configuration for an Anthropic environment.
// Decoded from raw config via DecodeConfig (in config.go).
// Struct size: 128 bytes (0x80), 8 fields.
//
// Field layout (from field access patterns across all methods):
//
//	offset 0x00: initScript string (ptr + len)
//	offset 0x10: stopHookPath string (ptr + len)
//	offset 0x20: cwd string (ptr + len)
//	offset 0x30: skillsDirectory string (ptr + len)
//	offset 0x40: environmentVariables map[string]string
//	offset 0x48: languages []anthropicLanguage (slice: ptr + len + cap)
//	offset 0x60: bakuProjectConfig *bakuProjectConfig
//	offset 0x68: devServerConfig *devServerConfig
//	offset 0x70: padding/reserved
type anthropicConfig struct {
	InitScript           string              `json:"init_script,omitempty"`
	StopHookPath         string              `json:"stop_hook_path,omitempty"`
	CWD                  string              `json:"cwd"`
	SkillsDirectory      string              `json:"skills_directory,omitempty"`
	EnvironmentVariables map[string]string    `json:"environment_variables,omitempty"`
	Languages            []anthropicLanguage  `json:"languages,omitempty"`
	BakuProjectConfig    *bakuProjectConfig   `json:"baku_project,omitempty"`
	DevServerConfig      *devServerConfig     `json:"dev_server,omitempty"`
}

// anthropicLanguage represents a language runtime to install.
type anthropicLanguage struct {
	Name    string `json:"name"`
	Version string `json:"version"`
}

// bakuProjectConfig holds Baku project configuration.
type bakuProjectConfig struct {
	ProjectID string `json:"project_id,omitempty"`
}

// devServerConfig holds dev server configuration.
type devServerConfig struct {
	Command string `json:"command,omitempty"`
	Port    int    `json:"port,omitempty"`
}

// DecodeConfig decodes a raw configuration value into an anthropicConfig.
// It uses encoding/json.Unmarshal to populate the config struct, then
// validates that the required 'cwd' field is present.
//
// Binary address: 0xb04020
// Source file: config.go (per objdump source annotation)
//
// Assembly flow:
//  1. runtime.newobject to allocate anthropicConfig
//  2. encoding/json.Unmarshal(rawConfig as []byte, &cfg)
//  3. If unmarshal error: fmt.Errorf("to unmarshal anthropic config: %w", err) (0x28=40 chars)
//  4. Check cfg.CWD != "" (CMPQ 0x28(AX), $0x0)
//  5. If empty: fmt.Errorf("cwd field is required in environment configuration") (0x32=50 chars)
//  6. Return (cfg, nil)
func DecodeConfig(rawConfig interface{}) (*anthropicConfig, error) {
	cfg := &anthropicConfig{}

	// rawConfig is expected to be a []byte (JSON)
	data, ok := rawConfig.([]byte)
	if !ok {
		jsonData, err := json.Marshal(rawConfig)
		if err != nil {
			return nil, fmt.Errorf("to unmarshal anthropic config: %w", err)
		}
		data = jsonData
	}

	if err := json.Unmarshal(data, cfg); err != nil {
		return nil, fmt.Errorf("to unmarshal anthropic config: %w", err)
	}

	if cfg.CWD == "" {
		return nil, fmt.Errorf("cwd field is required in environment configuration")
	}

	return cfg, nil
}

// New creates a new Anthropic environment type instance. It first decodes the
// raw config via DecodeConfig, then allocates an anthropicEnvironmentType with
// the decoded config and logger.
//
// Binary address: 0xaf8540
// Source file: anthropic.go
//
// Assembly flow:
//   1. Calls DecodeConfig(rawConfig) at 0xaf8560
//   2. If error, returns (nil, error) at 0xaf85c9
//   3. Allocates anthropicEnvironmentType via runtime.newobject at 0xaf8576
//   4. Sets config and logger fields, zeroes startupContext
//   5. Returns interface via itab at 0xaf85bc
func New(logger *slog.Logger, rawConfig interface{}) (envtype.EnvironmentType, error) {
	cfg, err := DecodeConfig(rawConfig)
	if err != nil {
		return nil, err
	}

	env := &anthropicEnvironmentType{
		config: cfg,
		logger: logger,
	}
	return env, nil
}

// SetStartupContext sets the startup context on the environment.
//
// Binary address: 0xaf8620
// Source file: anthropic.go
// Assembly: simple field store at offset 0x10 of receiver.
func (e *anthropicEnvironmentType) SetStartupContext(ctx *config.StartupContext) {
	e.startupContext = ctx
}

// SetAuthContext sets the authentication context on the environment.
//
// Binary address: 0xaf8680
// Source file: anthropic.go
// Assembly: stores interface (itab, data) at offset 0x18 of receiver.
func (e *anthropicEnvironmentType) SetAuthContext(authCtx interface{}) {
	e.authContext = authCtx
}

// SetSessionMode sets the session mode on the environment.
//
// Binary address: 0xaf86e0
// Source file: anthropic.go
// Assembly: stores SessionMode at offset 0x20 of receiver.
func (e *anthropicEnvironmentType) SetSessionMode(mode config.SessionMode) {
	e.sessionMode = mode
}

// GetCWD returns the current working directory for the Anthropic environment.
//
// Binary address: 0xafe8e0
// Source file: anthropic.go
// Assembly: returns string at offset 0x28 of receiver (2 words: ptr, len).
func (e *anthropicEnvironmentType) GetCWD() string {
	return e.cwd
}

// GetClaudeEnvironmentVariables returns the environment variables to be set
// for the Claude Code process. This includes API base URL, session metadata,
// MCP server configuration, and other session-specific variables.
//
// Binary address: 0xafe900
// Source file: anthropic.go
// Assembly: builds a map[string]string with various env var keys.
func (e *anthropicEnvironmentType) GetClaudeEnvironmentVariables() map[string]string {
	vars := make(map[string]string)

	// Copy all environment variables from the config's EnvironmentVariables map.
	// Assembly flow at 0xafe900:
	//  1. makemap_small()
	//  2. Load config (offset 0x00) -> load envvars map (offset 0x40)
	//  3. If nil, return empty map
	//  4. mapIterStart + mapIterNext loop copying all entries via mapassign_faststr
	if e.config != nil && e.config.EnvironmentVariables != nil {
		for k, v := range e.config.EnvironmentVariables {
			vars[k] = v
		}
	}

	return vars
}

// CreateLeaseManager creates a lease manager for the Anthropic environment.
// For Anthropic-hosted environments, this may return nil (no lease management needed).
//
// Binary address: not found as a distinct symbol for anthropic; may be inherited or nil.
func (e *anthropicEnvironmentType) CreateLeaseManager(ctx context.Context, sessionID string, workID string, apiBaseURL string) (envtype.LeaseManager, error) {
	return nil, nil
}

// Initialize performs the full initialization sequence for the Anthropic environment.
// This is a complex multi-step process that includes:
//   1. Installing languages (Go, Node, Python, etc.)
//   2. Cloning git repositories from sources
//   3. Running init scripts
//   4. Bootstrapping Claude skills and hooks
//   5. Setting up Baku projects
//   6. Starting dev servers
//   7. Checking if supervisord is running
//
// Binary address: 0xaf8740
// Source file: anthropic.go
//
// The function uses RecordFunction wrappers (func1-func6) for observability,
// each corresponding to a major initialization step.
func (e *anthropicEnvironmentType) Initialize(ctx context.Context) error {
	// Default session mode to "new" if not set.
	// Binary: 0xaf87db-0xaf8816 checks sessionMode len == 0, sets to "new"
	if e.sessionMode == "" {
		e.sessionMode = "new"
	}

	// Determine flags from config and startup context.
	hasInitScript := e.config != nil && e.config.InitScript != ""
	hasSources := e.startupContext != nil && len(e.startupContext.Sources) > 0

	// Log initialization start with configuration details.
	// Binary: 0xaf89e7 slog.Info "Initializing Anthropic environment" (34 chars)
	// with 10 attrs including cwd, session_mode, has_init_script, languages count, has_sources
	e.logger.Info("Initializing Anthropic environment",
		"cwd", e.cwd,
		"session_mode", e.sessionMode,
		"has_init_script", hasInitScript,
		"languages", len(e.config.Languages),
		"has_sources", hasSources,
	)

	// Check session mode: "new" or "setup-only" proceed with full init.
	// Binary: 0xaf8a20-0xaf8a70 compares session mode string
	isNewOrSetup := e.sessionMode == "new" || e.sessionMode == "setup-only"

	if isNewOrSetup && hasSources {
		// Step 1: Install languages (via RecordFunction.func1 at 0xafe420)
		if err := e.installLanguages(ctx); err != nil {
			return err
		}

		// Step 2: Sources processing is handled by the SourceHandlerManager
		// (via RecordFunction.func2 at 0xafdf60)
	} else if isNewOrSetup {
		// Step 1 only: Install languages without git
		if err := e.installLanguages(ctx); err != nil {
			return err
		}
	}

	// Step 3: Run init script (via RecordFunction.func3 at 0xafdaa0)
	if e.config != nil && e.config.InitScript != "" {
		if err := e.runInitScript(ctx, e.config.InitScript); err != nil {
			return err
		}
	}

	// Step 4: Bootstrap Claude skills (via RecordFunction.func4 at 0xafd5e0)
	if err := e.bootstrapClaudeSkills(ctx); err != nil {
		return err
	}

	// Step 5: Bootstrap hooks in all dirs (via RecordFunction.func5 at 0xafd120)
	if err := e.bootstrapHooksInAllDirs(ctx); err != nil {
		return err
	}

	// Step 6: Initialize Baku project and start dev server (via RecordFunction.func6 at 0xafcc60)
	if _, err := e.initializeBakuProject(ctx); err != nil {
		return err
	}

	return nil
}

// installLanguages installs configured languages (Node.js, Go, Python, etc.)
// concurrently using goroutines with a WaitGroup.
//
// Binary address: 0xafee00
// Source file: anthropic.go
func (e *anthropicEnvironmentType) installLanguages(ctx context.Context) error {
	// Log languages count.
	// Binary: 0xafeed1 "Installing languages" (20 chars) at Info level with language count attr
	e.logger.Info("Installing languages",
		"count", len(e.config.Languages),
	)

	startTime := time.Now()

	// Create WaitGroup and error channel for concurrent installation.
	// Binary: 0xafeeef runtime.newobject (sync.WaitGroup)
	// Binary: 0xafef18 runtime.makechan (error channel)
	var wg sync.WaitGroup
	errCh := make(chan error, len(e.config.Languages))

	// Spawn a goroutine for each language.
	// Binary: 0xafef38-0xaff0e8 loop with gowrap1 at 0xaff6c0
	for _, lang := range e.config.Languages {
		wg.Add(1)
		go func(language anthropicLanguage) {
			defer wg.Done()
			if err := e.installLanguage(ctx, language.Name, language.Version); err != nil {
				errCh <- err
			}
		}(lang)
	}

	// Wait for all installations to complete.
	// Binary: 0xaff0f5 sync.(*WaitGroup).Wait
	wg.Wait()

	elapsed := time.Since(startTime)

	// Close error channel and collect errors.
	// Binary: 0xaff127 runtime.closechan
	close(errCh)

	var errors []error
	for err := range errCh {
		errors = append(errors, err)
	}

	if len(errors) > 0 {
		// Binary: 0xaff45e level=8 (Error), "Failed to install some languages" (32 chars)
		errMsg := fmt.Sprintf("failed to install %d language(s)", len(errors))
		e.logger.Error("Failed to install some languages",
			"error_count", len(errors),
			"duration", elapsed,
			"languages", len(e.config.Languages),
			"errors", fmt.Sprintf("%v", errors),
		)
		return fmt.Errorf("%s", errMsg)
	}

	// Binary: 0xaff4d4+ Log success with duration
	e.logger.Info("Languages installed successfully",
		"count", len(e.config.Languages),
		"duration", elapsed,
	)

	return nil
}

// installLanguage installs a single language runtime by selecting the
// appropriate install script, substituting the version placeholder ($1),
// and executing it via process.ExecuteScript.
//
// Binary address: 0xaffe80
// Source file: anthropic.go
//
// Assembly flow:
//  1. slog.Info "Installing language" (0x13=19 chars) with name, version, script_len, is_resume attrs at 0xafffcd
//  2. Switch on language name:
//     - "go" / "golang" -> install_scripts.goScript
//     - "node" / "nodejs" -> install_scripts.nodeScript
//     - "python" -> install_scripts.pythonScript
//     - other -> logs warning "No install script for language" and returns nil
//  3. strings.Replace(script, "$1", version, -1) at 0xb00060
//  4. Creates temp file pattern via fmt.Sprintf("install-%s-%s-*.sh", name, version) at 0xb0026d
//  5. Calls process.ExecuteScript(ctx, logger, scriptContent, pattern, streamer) at 0xb002a9
//  6. On error: returns fmt.Errorf("failed to execute installation script: %w", err)
//  7. On result.Error != nil: returns fmt.Errorf("init script failed: %w", result.Error)
//  8. On success: logs "Installation script completed" and checks version output
//  9. Iterates over stdout lines, checks if installed version matches expected
// 10. If no version match found: logs error "failed to install %s %s: %s (exit code: %d)"
func (e *anthropicEnvironmentType) installLanguage(ctx context.Context, name, version string) error {
	// 0xafffcd: slog.Info "Installing language"
	e.logger.Info("Installing language",
		"name", name,
		"version", version,
		"script_len", 0,
		"is_resume", e.sessionMode == "resume",
	)

	// 0xafffd2-0xb000d8: Select install script based on language name
	var script string
	switch name {
	case "go", "golang":
		script = install_scripts.GoScript
	case "node", "nodejs":
		script = install_scripts.NodeScript
	case "python":
		script = install_scripts.PythonScript
	default:
		// 0xb00ae0: slog.Error "No install script for language"
		e.logger.Error("No install script for language",
			"name", name,
			"version", version,
		)
		return nil
	}

	// 0xb00060: strings.Replace(script, "$1", version, -1)
	script = strings.Replace(script, "$1", version, -1)

	// 0xb0026d: fmt.Sprintf("install-%s-%s-*.sh", name, version)
	pattern := fmt.Sprintf("install-%s-%s-*.sh", name, version)

	// 0xb002a9: process.ExecuteScript
	result, err := process.ExecuteScript(ctx, e.logger, script, pattern, nil)
	if err != nil {
		// 0xb003ea: slog + fmt.Errorf "failed to execute installation script: %w"
		e.logger.Error("Failed to execute installation script",
			"name", name,
			"version", version,
			"error", err,
		)
		return fmt.Errorf("failed to execute installation script: %w", err)
	}

	// 0xafeb91: Check result.Error (offset 0x40)
	if result.Error != nil {
		// 0xb009e3: fmt.Errorf with name, version, output, exit code
		return fmt.Errorf("failed to install %s %s: %s (exit code: %d)",
			name, version, result.Error.Error(), result.ExitCode)
	}

	// 0xb0062c: slog.Info "Installation script completed"
	e.logger.Info("Installation script completed",
		"name", name,
		"version", version,
		"exit_code", result.ExitCode,
		"duration", result.Duration,
		"is_resume", e.sessionMode == "resume",
	)

	return nil
}

// initGitRepo initializes a git repository in the given directory,
// configures git settings, adds all files, and creates an initial commit.
//
// Binary address: 0xb02600
// Source file: anthropic.go
//
// Assembly flow:
//  1. Builds 7 command slices as allocated structs (runtime.newobject):
//     a. ["git", "init"]
//     b. ["git", "config", "user.email", "claude@anthropic.com"]
//     c. ["git", "config", "user.name", "Claude"]
//     d. ["git", "config", "gc.auto", "0"]
//     e. ["git", "config", "commit.gpgsign", "false"]
//     f. ["git", "add", "."]
//     g. ["git", "commit", "-m", "Initial project from Baku template"]
//  2. Iterates over all 7 commands (CMPQ AX, $0x7 at 0xb029c0)
//  3. For each: exec.CommandContext(ctx, args[0], args[1:]...)
//     Sets cmd.Dir to the dir parameter (offset 0x40/0x48 of exec.Cmd)
//  4. Calls cmd.CombinedOutput()
//  5. On error: fmt.Errorf("git command %v failed: %w\nOutput: %s", args, err, output)
//  6. On success (all 7 pass): slog.Info "Git repository initialized" with "dir" attr
func (e *anthropicEnvironmentType) initGitRepo(ctx context.Context, dir string) error {
	// 0xb02680-0xb02973: Build 7 git command arg slices
	commands := [][]string{
		{"git", "init"},
		{"git", "config", "user.email", "claude@anthropic.com"},
		{"git", "config", "user.name", "Claude"},
		{"git", "config", "gc.auto", "0"},
		{"git", "config", "commit.gpgsign", "false"},
		{"git", "add", "."},
		{"git", "commit", "-m", "Initial project from Baku template"},
	}

	// 0xb029c0-0xb02b4f: Iterate and execute each command
	for _, args := range commands {
		cmd := exec.CommandContext(ctx, args[0], args[1:]...)
		cmd.Dir = dir

		output, err := cmd.CombinedOutput()
		if err != nil {
			// 0xb02b2d: fmt.Errorf("git command %v failed: %w\nOutput: %s", ...)
			return fmt.Errorf("git command %v failed: %w\nOutput: %s", args, err, string(output))
		}
	}

	// 0xb02bdb: slog.Info "Git repository initialized"
	e.logger.Info("Git repository initialized",
		"dir", dir,
	)

	return nil
}

// runInitScript runs the user-specified init script if configured.
// It receives the init script content and script length as parameters
// from the RecordFunction wrapper (func3).
//
// Binary address: 0xafea20
// Source file: anthropic.go
//
// Assembly flow:
//  1. slog.Info "Running initialization script" (0x1d=29 chars) with "script_len" attr at 0xafead7
//  2. Creates func1 closure capturing receiver at 0xafeae3-0xafeaef
//  3. Calls process.ExecuteScript(ctx, logger, scriptContent, "init-script-*.sh", streamer) at 0xafeb4d
//  4. If ExecuteScript error: fmt.Errorf("failed to execute init script: %w", err) at 0xafeb86
//  5. If result.Error != nil (offset 0x40): fmt.Errorf("init script failed: %w", result.Error) at 0xafebd2
//  6. On success: slog.Info "Successfully executed init script" (0x21=33 chars) at 0xafec02
func (e *anthropicEnvironmentType) runInitScript(ctx context.Context, scriptContent string) error {
	// 0xafead7: slog.Info "Running initialization script"
	e.logger.Info("Running initialization script",
		"script_len", len(scriptContent),
	)

	// 0xafeb4d: process.ExecuteScript with pattern "init-script-*.sh"
	result, err := process.ExecuteScript(ctx, e.logger, scriptContent, "init-script-*.sh", nil)
	if err != nil {
		// 0xafeb86: fmt.Errorf("failed to execute init script: %w", err)
		return fmt.Errorf("failed to execute init script: %w", err)
	}

	// 0xafeb91: Check result.Error (offset 0x40 of Result struct)
	if result.Error != nil {
		// 0xafebd2: fmt.Errorf("init script failed: %w", result.Error)
		return fmt.Errorf("init script failed: %w", result.Error)
	}

	// 0xafec02: slog.Info "Successfully executed init script"
	e.logger.Info("Successfully executed init script")

	return nil
}

// bootstrapClaudeSkills sets up Claude Code skills in the home directory
// and the configured working directory ("/home/claude").
//
// Binary address: 0xb01d00
// Source file: anthropic.go
//
// Assembly flow:
//  1. os.UserHomeDir() at 0xb01d2a
//  2. If error: fmt.Errorf("failed to get home directory: %w", err) at 0xb01d51
//  3. Call bootstrapClaudeSkillsUnderDir(homeDir) at 0xb01dc7
//  4. If error: fmt.Errorf("failed to bootstrap Claude skills in home dir %s: %w", ...) at 0xb01e04
//  5. Call bootstrapClaudeSkillsUnderDir("/home/claude") at 0xb01e68
//  6. If error: fmt.Errorf("failed to bootstrap Claude skills in cwd %s: %w", ...) at 0xb01e8a
func (e *anthropicEnvironmentType) bootstrapClaudeSkills(ctx context.Context) error {
	// 0xb01d2a: os.UserHomeDir()
	homeDir, err := os.UserHomeDir()
	if err != nil {
		// 0xb01d51: fmt.Errorf("failed to get home directory: %w", err)
		return fmt.Errorf("failed to get home directory: %w", err)
	}

	// 0xb01dc7: bootstrapClaudeSkillsUnderDir(homeDir)
	if err := e.bootstrapClaudeSkillsUnderDir(ctx, homeDir); err != nil {
		// 0xb01e04: fmt.Errorf with homeDir
		return fmt.Errorf("failed to bootstrap Claude skills in home dir %s: %w", homeDir, err)
	}

	// 0xb01e40: hardcoded second dir = "/home/claude" (0xc=12 chars)
	if err := e.bootstrapClaudeSkillsUnderDir(ctx, "/home/claude"); err != nil {
		// 0xb01e8a: fmt.Errorf with cwd
		return fmt.Errorf("failed to bootstrap Claude skills in cwd %s: %w", "/home/claude", err)
	}

	return nil
}

// bootstrapClaudeSkillsUnderDir creates the .claude/skills/session-start-hook/
// directory under the given dir and writes the session start hook skill YAML
// as SKILL.md.
//
// Binary address: 0xb01ee0
// Source file: anthropic.go
//
// Assembly flow:
//  1. filepath.Join(dir, ".claude", "skills", "session-start-hook") at 0xb01f64-0xb01fba
//  2. os.MkdirAll(skillDir, 0755) at 0xb01fc0 (perm 0x1ed = 0o755)
//  3. If error: fmt.Errorf("failed to create skills directory: %w", err) at 0xb02056
//  4. filepath.Join(skillDir, "SKILL.md") at 0xb01ff6-0xb02020
//  5. os.WriteFile(skillPath, []byte(sessionStartHookSkill), 0600) at 0xb02078 (perm 0x180 = 0o600)
//  6. If error: fmt.Errorf("failed to write skill file: %w", err) at 0xb020e9
//  7. On success: slog.Info "Bootstrapped Claude skills under directory" at 0xb02192
//  8. Or if skill already exists: slog.Info "Claude skills already exist, skipping" at 0xb0223d
func (e *anthropicEnvironmentType) bootstrapClaudeSkillsUnderDir(ctx context.Context, dir string) error {
	// 0xb01f64: filepath.Join(dir, ".claude", "skills", "session-start-hook")
	skillDir := filepath.Join(dir, ".claude", "skills", "session-start-hook")

	// 0xb01ff6: filepath.Join(skillDir, "SKILL.md")
	skillPath := filepath.Join(skillDir, "SKILL.md")

	// Check if skill file already exists
	if _, err := os.Stat(skillPath); err == nil {
		// 0xb0223d: slog.Info "Claude skills already exist, skipping"
		e.logger.Info("Claude skills already exist, skipping",
			"path", skillPath,
		)
		return nil
	}

	// 0xb01fc0: os.MkdirAll(skillDir, 0755)
	if err := os.MkdirAll(skillDir, 0755); err != nil {
		// 0xb02056: fmt.Errorf("failed to create skills directory: %w", err)
		return fmt.Errorf("failed to create skills directory: %w", err)
	}

	// 0xb02078: os.WriteFile(skillPath, sessionStartHookSkill, 0600)
	if err := os.WriteFile(skillPath, []byte(sessionStartHookSkill), 0600); err != nil {
		// 0xb020e9: fmt.Errorf("failed to write skill file: %w", err)
		return fmt.Errorf("failed to write skill file: %w", err)
	}

	// 0xb02192: slog.Info "Bootstrapped Claude skills under directory"
	e.logger.Info("Bootstrapped Claude skills under directory",
		"path", skillPath,
	)

	return nil
}

// bootstrapHooksInAllDirs installs Claude settings and stop hooks in the
// home directory and the hardcoded "/home/claude" directory.
//
// Binary address: 0xb01480
// Source file: anthropic.go
//
// Assembly flow:
//  1. os.UserHomeDir() at 0xb014b2
//  2. If error: fmt.Errorf("failed to get home directory: %w", err) at 0xb014e2
//  3. Copies 64 bytes of stack args (defaultSettingsJSON + stopHookScript slices) at 0xb0150f-0xb0153d
//  4. Call bootstrapHooksUnderDir(homeDir) at 0xb01560
//  5. If error: fmt.Errorf("failed to bootstrap hooks in home dir %s: %w", ...) at 0xb015e5
//  6. Call bootstrapHooksUnderDir("/home/claude") at 0xb01660 (DI = LEAQ "/home/claude", SI = 0xc)
//  7. If error: fmt.Errorf("failed to bootstrap hooks in cwd %s: %w", ...) at 0xb016b7
func (e *anthropicEnvironmentType) bootstrapHooksInAllDirs(ctx context.Context) error {
	// 0xb014b2: os.UserHomeDir()
	homeDir, err := os.UserHomeDir()
	if err != nil {
		// 0xb014e2: fmt.Errorf("failed to get home directory: %w", err)
		return fmt.Errorf("failed to get home directory: %w", err)
	}

	// 0xb01560: bootstrapHooksUnderDir(homeDir, defaultSettingsJSON, stopHookScript)
	if err := e.bootstrapHooksUnderDir(ctx, homeDir); err != nil {
		// 0xb015e5: fmt.Errorf with homeDir and error
		return fmt.Errorf("failed to bootstrap hooks in home dir %s: %w", homeDir, err)
	}

	// 0xb01660: bootstrapHooksUnderDir("/home/claude", ...)
	if err := e.bootstrapHooksUnderDir(ctx, "/home/claude"); err != nil {
		// 0xb016b7: fmt.Errorf with dir and error
		return fmt.Errorf("failed to bootstrap hooks in cwd %s: %w", "/home/claude", err)
	}

	return nil
}

// bootstrapHooksUnderDir writes the default settings JSON and stop hook script
// under dir/.claude/. The settings are written as settings.json and the stop
// hook is written with the path from config.StopHookPath. Both files are only
// written if they don't already exist (checked via os.Stat).
//
// Binary address: 0xb01720
// Source file: anthropic.go
//
// Assembly flow:
//  1. claudeDir = filepath.Join(dir, ".claude") at 0xb01770-0xb017a5
//  2. settingsPath = filepath.Join(claudeDir, "settings.json") at 0xb017ee-0xb01806
//  3. stopHookPath = filepath.Join(claudeDir, config.StopHookPath) at 0xb01849-0xb01871
//     (StopHookPath loaded from stack args at 0x1b0(SP)/0x1b8(SP))
//  4. os.MkdirAll(claudeDir, 0755) at 0xb0188f (perm 0x1ed = 0o755)
//  5. If MkdirAll error: fmt.Errorf("failed to create .claude directory: %w", err) at 0xb018bf
//  6. os.Stat(settingsPath) at 0xb018ec
//  7. If stat error (doesn't exist):
//     - os.WriteFile(settingsPath, defaultSettingsJSON, 0600) at 0xb01922 (perm 0x180)
//     - If error: fmt.Errorf("failed to write settings.json: %w", err) at 0xb01952
//     - slog.Info "Wrote default Claude settings" at 0xb01a00
//  8. If stat success (exists):
//     - slog.Info "Claude settings already exist, skipping" at 0xb01ab0
//  9. os.Stat(stopHookPath) at 0xb01ad6
// 10. If stat error (doesn't exist):
//     - os.WriteFile(stopHookPath, stopHookScript, 0755) at 0xb01b11 (perm 0x1ed)
//     - If error: fmt.Errorf("failed to write stop hook script: %w", err) at 0xb01b41
//     - slog.Info "Wrote stop hook script" at 0xb01be9
// 11. If stat success (exists):
//     - slog.Info "Stop hook script already exists, skipping" at 0xb01c95
func (e *anthropicEnvironmentType) bootstrapHooksUnderDir(ctx context.Context, dir string) error {
	// 0xb01770: claudeDir = filepath.Join(dir, ".claude")
	claudeDir := filepath.Join(dir, ".claude")

	// 0xb017ee: settingsPath = filepath.Join(claudeDir, "settings.json")
	settingsPath := filepath.Join(claudeDir, "settings.json")

	// 0xb01849: stopHookPath = filepath.Join(claudeDir, config.StopHookPath)
	stopHookPath := filepath.Join(claudeDir, e.config.StopHookPath)

	// 0xb0188f: os.MkdirAll(claudeDir, 0755)
	if err := os.MkdirAll(claudeDir, 0755); err != nil {
		// 0xb018bf: fmt.Errorf("failed to create .claude directory: %w", err)
		return fmt.Errorf("failed to create .claude directory: %w", err)
	}

	// 0xb018ec: os.Stat(settingsPath) - check if settings already exist
	if _, err := os.Stat(settingsPath); err != nil {
		// File doesn't exist, write it
		// 0xb01922: os.WriteFile(settingsPath, defaultSettingsJSON, 0600)
		if err := os.WriteFile(settingsPath, defaultSettingsJSON, 0600); err != nil {
			// 0xb01952: fmt.Errorf("failed to write settings.json: %w", err)
			return fmt.Errorf("failed to write settings.json: %w", err)
		}
		// 0xb01a00: slog.Info "Wrote default Claude settings"
		e.logger.Info("Wrote default Claude settings",
			"path", settingsPath,
		)
	} else {
		// 0xb01ab0: slog.Info "Claude settings already exist, skipping"
		e.logger.Info("Claude settings already exist, skipping",
			"path", settingsPath,
		)
	}

	// 0xb01ad6: os.Stat(stopHookPath) - check if stop hook already exists
	if _, err := os.Stat(stopHookPath); err != nil {
		// File doesn't exist, write it
		// 0xb01b11: os.WriteFile(stopHookPath, stopHookScript, 0755)
		if err := os.WriteFile(stopHookPath, stopHookScript, 0755); err != nil {
			// 0xb01b41: fmt.Errorf("failed to write stop hook script: %w", err)
			return fmt.Errorf("failed to write stop hook script: %w", err)
		}
		// 0xb01be9: slog.Info "Wrote stop hook script"
		e.logger.Info("Wrote stop hook script",
			"path", stopHookPath,
		)
	} else {
		// 0xb01c95: slog.Info "Stop hook script already exists, skipping"
		e.logger.Info("Stop hook script already exists, skipping",
			"path", stopHookPath,
		)
	}

	return nil
}

// initializeBakuProject initializes a new Baku project by copying the
// vite-template, initializing a git repo, and starting the dev server.
//
// Binary address: 0xb022c0
// Source file: anthropic.go
//
// Assembly flow:
//  1. slog.Debug "Initializing Baku project" (0x19=25 chars) with 4 attrs
//     (itab/runtime pointers for debug level attrs) at 0xb0236d
//  2. copyDir("/opt/baku-templates/vite-template", "/home/claude/project") at 0xb0238a
//  3. If error: fmt.Errorf("failed to copy Baku template: %w", err) at 0xb023cf
//  4. initGitRepo(ctx, "/home/claude/project") at 0xb0240b
//  5. If error: fmt.Errorf("failed to initialize git repo: %w", err) at 0xb02452
//  6. startDevServer(ctx, "/home/claude/project") at 0xb0248e
//  7. If startDevServer error: slog.Error "Failed to start dev server (non-fatal)"
//     with "error" attr at 0xb0250b (error is logged but NOT returned)
//  8. Return ("/home/claude/project", nil) at 0xb02517
func (e *anthropicEnvironmentType) initializeBakuProject(ctx context.Context) (string, error) {
	// 0xb0236d: slog.Debug "Initializing Baku project"
	e.logger.Debug("Initializing Baku project")

	// 0xb0238a: copyDir("/opt/baku-templates/vite-template", "/home/claude/project")
	if err := copyDir("/opt/baku-templates/vite-template", "/home/claude/project"); err != nil {
		// 0xb023cf: fmt.Errorf("failed to copy Baku template: %w", err)
		return "", fmt.Errorf("failed to copy Baku template: %w", err)
	}

	// 0xb0240b: initGitRepo(ctx, "/home/claude/project")
	if err := e.initGitRepo(ctx, "/home/claude/project"); err != nil {
		// 0xb02452: fmt.Errorf("failed to initialize git repo: %w", err)
		return "", fmt.Errorf("failed to initialize git repo: %w", err)
	}

	// 0xb0248e: startDevServer(ctx, "/home/claude/project")
	if err := e.startDevServer(ctx, "/home/claude/project"); err != nil {
		// 0xb0250b: slog.Error (non-fatal, just log it)
		e.logger.Error("Failed to start dev server (non-fatal)",
			"error", err,
		)
	}

	// 0xb02517: return "/home/claude/project"
	return "/home/claude/project", nil
}

// findExistingBakuProject checks if /home/claude/project exists as a directory.
// If it does, returns the path. Otherwise returns an error.
//
// Binary address: 0xb02560
// Source file: anthropic.go
//
// Assembly flow:
//  1. os.Stat("/home/claude/project") at 0xb02580 (LEAQ 0x310533 = "/home/claude/project", len 0x14=20)
//  2. If stat error (CX != nil): fmt.Errorf("project directory %s not found", path) at 0xb025c8
//  3. If stat success: check info.IsDir() via interface method call at 0xb02591
//  4. If not a directory: also returns the "not found" error
//  5. If is directory: return ("/home/claude/project", nil) at 0xb025e4
func (e *anthropicEnvironmentType) findExistingBakuProject(ctx context.Context) (string, error) {
	const projectDir = "/home/claude/project"

	// 0xb02580: os.Stat("/home/claude/project")
	info, err := os.Stat(projectDir)
	if err != nil || !info.IsDir() {
		// 0xb025c8: fmt.Errorf("project directory %s not found", projectDir)
		return "", fmt.Errorf("project directory %s not found", projectDir)
	}

	// 0xb025e4: return ("/home/claude/project", nil)
	return projectDir, nil
}

// startDevServer starts supervisord with /etc/supervisord.conf to run
// the dev server. If supervisord is already running, it logs and returns.
// The actual supervisord process is started asynchronously via a goroutine
// that waits for it to complete.
//
// Binary address: 0xb02c60
// Source file: anthropic.go
//
// Assembly flow:
//  1. isSupervisordRunning() at 0xb02ca2
//  2. If running: slog.Info "supervisord already running, skipping start" at 0xb02f30
//     then return (nil, nil)
//  3. exec.Command("supervisord", "-c", "/etc/supervisord.conf") at 0xb02d13
//  4. cmd.Start() at 0xb02d20
//  5. If start error: return fmt.Errorf("failed to start supervisord: %w", err) at 0xb02d65
//  6. slog.Info "supervisord started for dev server" with "dir", "pid" attrs at 0xb02e4e
//  7. go func1() { cmd.Wait(); if err { slog.Error(...) } }() at 0xb02eea
//  8. return (nil, nil) at 0xb02eef
func (e *anthropicEnvironmentType) startDevServer(ctx context.Context, dir string) error {
	// 0xb02ca2: isSupervisordRunning()
	if e.isSupervisordRunning() {
		// 0xb02f30: slog.Info "supervisord already running, skipping start"
		e.logger.Info("supervisord already running, skipping start")
		return nil
	}

	// 0xb02d13: exec.Command("supervisord", "-c", "/etc/supervisord.conf")
	cmd := exec.Command("supervisord", "-c", "/etc/supervisord.conf")

	// 0xb02d20: cmd.Start()
	if err := cmd.Start(); err != nil {
		// 0xb02d65: fmt.Errorf("failed to start supervisord: %w", err)
		return fmt.Errorf("failed to start supervisord: %w", err)
	}

	// 0xb02e4e: slog.Info "supervisord started for dev server"
	e.logger.Info("supervisord started for dev server",
		"dir", dir,
		"pid", cmd.Process.Pid,
	)

	// 0xb02eea: go func1() - wait for supervisord in background
	go func() {
		if err := cmd.Wait(); err != nil {
			e.logger.Error("supervisord exited with error",
				"dir", dir,
				"error", err,
			)
		}
	}()

	return nil
}

// isSupervisordRunning checks if supervisord is running in the environment.
// It attempts a TCP connection to 127.0.0.1:9199 with a 1-second timeout.
// Returns true if the connection succeeds, false otherwise.
//
// Binary address: 0xb03120
// Source file: anthropic.go (line 900)
//
// Assembly flow:
//  1. net.DialTimeout("tcp", "127.0.0.1:9199", 1*time.Second) at 0xb0314b
//  2. If error (CX != nil): return false
//  3. If success: call conn.Close() via interface method, return true
func (e *anthropicEnvironmentType) isSupervisordRunning() bool {
	conn, err := net.DialTimeout("tcp", "127.0.0.1:9199", 1*time.Second)
	if err != nil {
		return false
	}
	conn.Close()
	return true
}

// copyDir recursively copies a directory from src to dst.
// Uses filepath.WalkDir with a closure (func1 at 0xb03320) that copies
// each file and recreates directory structure.
//
// Binary address: 0xb03180
// Source file: anthropic.go (line 914)
//
// Assembly flow:
//  1. Build closure capturing src and dst strings
//  2. filepath.WalkDir(src, closureFunc)
//  3. If WalkDir error: fmt.Errorf("failed to copy directory %s to %s: %w", src, dst, err) (0x25=37 chars)
//  4. Return nil on success
func copyDir(src, dst string) error {
	err := filepath.WalkDir(src, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}

		// Compute relative path and target path.
		relPath, err := filepath.Rel(src, path)
		if err != nil {
			return err
		}
		targetPath := filepath.Join(dst, relPath)

		if d.IsDir() {
			return os.MkdirAll(targetPath, 0755)
		}

		// Copy file contents.
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}

		info, err := d.Info()
		if err != nil {
			return err
		}

		return os.WriteFile(targetPath, data, info.Mode())
	})

	if err != nil {
		return fmt.Errorf("failed to copy directory %s to %s: %w", src, dst, err)
	}

	return nil
}

// Ensure unused imports are referenced.
var (
	_ = o11y.RecordFunctionDeferred
	_ = process.ExecuteScript
)
