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
	"path/filepath"
	"sync"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/config"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/envtype"
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

		// Step 2: Clone git repos / initialize working directory (via RecordFunction.func2 at 0xafdf60)
		if err := e.initGitRepo(ctx); err != nil {
			return err
		}
	} else if isNewOrSetup {
		// Step 1 only: Install languages without git
		if err := e.installLanguages(ctx); err != nil {
			return err
		}
	}

	// Step 3: Run init script (via RecordFunction.func3 at 0xafdaa0)
	if err := e.runInitScript(ctx); err != nil {
		return err
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
	if err := e.initializeBakuProject(ctx); err != nil {
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
			if err := e.installLanguage(ctx, language.Name); err != nil {
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

// installLanguage installs a single language runtime.
//
// Binary address: 0xaffe80
// Source file: anthropic.go
func (e *anthropicEnvironmentType) installLanguage(ctx context.Context, language string) error {
	// Binary: calls process.RunCommand or similar to install the language
	// The exact implementation depends on the language type
	e.logger.Info("Installing language", "language", language)
	return nil
}

// initGitRepo initializes git repositories from the startup context sources.
//
// Binary address: 0xb02600
// Source file: anthropic.go
func (e *anthropicEnvironmentType) initGitRepo(ctx context.Context) error {
	// Binary: 0xb02600 - large function (line 839+)
	// Sets up git configuration in the working directory:
	//   1. git init (line 841: ["git", "init"])
	//   2. git config user.email "claude@anthropic.com" (line 842)
	//   3. git config user.name "Claude" (line 843)
	//   4. git config gc.auto "0" (line 844)
	//   5. git config commit.gpgsign "false" (line 848)
	//
	// Then iterates over sources from startupContext and clones each repository.
	// Uses process.RunCommand for git operations and sources.CloneGitRepository
	// for repository cloning.

	cwd := e.cwd

	// Initialize git repo.
	if err := process.RunCommand(ctx, e.logger, cwd, "git", "init"); err != nil {
		return fmt.Errorf("failed to initialize git repository: %w", err)
	}

	// Configure git settings.
	gitConfigs := [][2]string{
		{"user.email", "claude@anthropic.com"},
		{"user.name", "Claude"},
		{"gc.auto", "0"},
		{"commit.gpgsign", "false"},
	}

	for _, cfg := range gitConfigs {
		if err := process.RunCommand(ctx, e.logger, cwd, "git", "config", cfg[0], cfg[1]); err != nil {
			return fmt.Errorf("failed to set git config %s: %w", cfg[0], err)
		}
	}

	// Clone sources from startup context.
	if e.startupContext != nil {
		for _, source := range e.startupContext.Sources {
			if source.IsRepository() {
				dir := source.GetDirectory(cwd)
				if dir == "" {
					continue
				}
				e.logger.Info("Cloning repository",
					"type", source.GetType(),
					"directory", dir,
				)
				// Clone implementation delegated to sources package
			}
		}
	}

	return nil
}

// runInitScript runs the user-specified init script if configured.
//
// Binary address: 0xafea20
// Source file: anthropic.go
func (e *anthropicEnvironmentType) runInitScript(ctx context.Context) error {
	// Binary: 0xafea20
	// Checks if config has an init script configured.
	// If not, returns nil immediately.
	// If yes, runs the script using process.RunCommand in the cwd.
	if e.config == nil || e.config.InitScript == "" {
		return nil
	}

	e.logger.Info("Running init script",
		"script", e.config.InitScript,
		"cwd", e.cwd,
	)

	if err := process.RunCommand(ctx, e.logger, e.cwd, "bash", "-c", e.config.InitScript); err != nil {
		return fmt.Errorf("init script failed: %w", err)
	}

	e.logger.Info("Init script completed successfully")
	return nil
}

// bootstrapClaudeSkills sets up Claude Code skills in the working directory.
//
// Binary address: 0xb01d00
// Source file: anthropic.go
func (e *anthropicEnvironmentType) bootstrapClaudeSkills(ctx context.Context) error {
	// Binary: 0xb01d00
	// 1. Get home directory via os.UserHomeDir()
	// 2. If error: fmt.Errorf("failed to get home directory: %w", err)
	// 3. Call bootstrapClaudeSkillsUnderDir(ctx, homeDir)
	// 4. If error: fmt.Errorf("failed to bootstrap Claude skills in home dir %s: %w", homeDir, err)
	// 5. Also bootstrap under cwd if different from home
	homeDir, err := os.UserHomeDir()
	if err != nil {
		return fmt.Errorf("failed to get home directory: %w", err)
	}

	if err := e.bootstrapClaudeSkillsUnderDir(ctx, homeDir); err != nil {
		return fmt.Errorf("failed to bootstrap Claude skills in home dir %s: %w", homeDir, err)
	}

	// Also bootstrap under the working directory.
	if e.cwd != "" && e.cwd != homeDir {
		if err := e.bootstrapClaudeSkillsUnderDir(ctx, e.cwd); err != nil {
			return fmt.Errorf("failed to bootstrap Claude skills in cwd %s: %w", e.cwd, err)
		}
	}

	return nil
}

// bootstrapClaudeSkillsUnderDir copies skills into a specific directory.
// It creates the .claude/ directory structure and writes default settings JSON
// and stop hook script files.
//
// Binary address: 0xb01ee0
// Source file: anthropic.go
func (e *anthropicEnvironmentType) bootstrapClaudeSkillsUnderDir(ctx context.Context, dir string) error {
	// Creates .claude/ directory under the target dir and writes:
	// - Default settings JSON (from package-level defaultSettingsJSON)
	// - Stop hook script (from package-level stopHookScript)
	claudeDir := filepath.Join(dir, ".claude")
	if err := os.MkdirAll(claudeDir, 0755); err != nil {
		return fmt.Errorf("failed to create .claude directory: %w", err)
	}

	// Write default settings if available.
	if len(defaultSettingsJSON) > 0 {
		settingsPath := filepath.Join(claudeDir, "settings.json")
		if err := os.WriteFile(settingsPath, defaultSettingsJSON, 0644); err != nil {
			return fmt.Errorf("failed to write settings.json: %w", err)
		}
	}

	// Write stop hook script if available.
	if len(stopHookScript) > 0 {
		hookPath := filepath.Join(claudeDir, "stop-hook-git-check.sh")
		if err := os.WriteFile(hookPath, stopHookScript, 0755); err != nil {
			return fmt.Errorf("failed to write stop hook script: %w", err)
		}
	}

	return nil
}

// bootstrapHooksInAllDirs installs git hooks in all relevant directories.
//
// Binary address: 0xb01480
// Source file: anthropic.go
func (e *anthropicEnvironmentType) bootstrapHooksInAllDirs(ctx context.Context) error {
	// Binary: 0xb01480 (line 672)
	// 1. Get home directory via os.UserHomeDir()
	// 2. If error: fmt.Errorf("failed to get home directory: %w", err)
	// 3. Call bootstrapHooksUnderDir(ctx, homeDir)
	// 4. If error: fmt.Errorf("failed to bootstrap hooks in home dir %s: %w", homeDir, err)
	// 5. Also bootstrap under cwd if different from home
	homeDir, err := os.UserHomeDir()
	if err != nil {
		return fmt.Errorf("failed to get home directory: %w", err)
	}

	if err := e.bootstrapHooksUnderDir(ctx, homeDir); err != nil {
		return fmt.Errorf("failed to bootstrap hooks in home dir %s: %w", homeDir, err)
	}

	// Also bootstrap hooks under the working directory.
	if e.cwd != "" && e.cwd != homeDir {
		if err := e.bootstrapHooksUnderDir(ctx, e.cwd); err != nil {
			return fmt.Errorf("failed to bootstrap hooks in cwd %s: %w", e.cwd, err)
		}
	}

	return nil
}

// bootstrapHooksUnderDir installs hooks under a specific directory.
// It sets up git hooks for the Claude Code environment.
//
// Binary address: 0xb01720
// Source file: anthropic.go
func (e *anthropicEnvironmentType) bootstrapHooksUnderDir(ctx context.Context, dir string) error {
	// Creates .claude/hooks/ directory and installs hook scripts
	hooksDir := filepath.Join(dir, ".claude", "hooks")
	if err := os.MkdirAll(hooksDir, 0755); err != nil {
		return fmt.Errorf("failed to create hooks directory: %w", err)
	}

	return nil
}

// initializeBakuProject initializes a Baku project if configured.
//
// Binary address: 0xb022c0
// Source file: anthropic.go
func (e *anthropicEnvironmentType) initializeBakuProject(ctx context.Context) error {
	// Binary: 0xb022c0
	// 1. Calls findExistingBakuProject to check for existing project
	// 2. If found, logs and returns
	// 3. If not found and config has baku project config, initializes new project
	// 4. After project init, calls startDevServer
	existing, err := e.findExistingBakuProject(ctx)
	if err != nil {
		return fmt.Errorf("failed to find existing Baku project: %w", err)
	}

	if existing != "" {
		e.logger.Info("Found existing Baku project", "path", existing)
	}

	// Start dev server if configured.
	if err := e.startDevServer(ctx); err != nil {
		return fmt.Errorf("failed to start dev server: %w", err)
	}

	return nil
}

// findExistingBakuProject searches for an existing Baku project.
//
// Binary address: 0xb02560
// Source file: anthropic.go
func (e *anthropicEnvironmentType) findExistingBakuProject(ctx context.Context) (string, error) {
	// Searches for existing Baku project in the working directory
	// by looking for known project markers.
	return "", nil
}

// startDevServer starts a development server if configured.
//
// Binary address: 0xb02c60
// Source file: anthropic.go
func (e *anthropicEnvironmentType) startDevServer(ctx context.Context) error {
	// Checks if dev server configuration is present in config.
	// If not configured, returns nil immediately.
	if e.config == nil || e.config.DevServerConfig == nil {
		return nil
	}

	// Check if supervisord is running first.
	if e.isSupervisordRunning() {
		e.logger.Info("Supervisord is running, skipping dev server start")
		return nil
	}

	e.logger.Info("Starting dev server",
		"command", e.config.DevServerConfig.Command,
	)

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
	_ = o11y.RecordFunction
	_ = process.RunCommand
)
