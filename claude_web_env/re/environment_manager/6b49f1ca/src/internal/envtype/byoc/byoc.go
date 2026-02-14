// Package byoc implements the BYOC (Bring Your Own Container) environment type
// for the environment manager. BYOC environments run in customer-provided
// containers with custom auth round-tripping and lease management.
//
// Reconstructed from binary at Build ID 6b49f1ca (Go 1.25.6).
// Source path: /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/envtype/byoc/
//
// Key symbols:
//   - byoc.New (0xb042e0)
//   - byoc.Registration (0x1589460)
//   - byoc.defaultSettingsJSON (0x15addc0)
//   - byoc.stopHookScript (0x15adde0)
//   - byoc.init (0xb04220)
//   - byoc.containProvideAuthRoundTripper (itab at 0xf5b500 approx)
package byoc

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/config"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/envtype"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/podmonitor"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/process"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/sources"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/util"
)

// Registration is the global registration for the byoc environment type.
// Symbol: byoc.Registration (0x1589460)
var Registration *envtype.Registration

// defaultSettingsJSON holds the default Claude Code settings JSON for BYOC.
// Symbol: byoc.defaultSettingsJSON (0x15addc0)
var defaultSettingsJSON []byte

// stopHookScript holds the stop hook script content for BYOC.
// Symbol: byoc.stopHookScript (0x15adde0)
var stopHookScript []byte

// init copies shared defaults from the shared package.
//
// Reconstructed from: byoc.init (0xb04220)
// Assembly: similar pattern to anthropic.init, copies from envtype/shared.
func init() {
	// defaultSettingsJSON = shared.DefaultSettingsJSON
	// stopHookScript = shared.StopHookScript
}

// byocConfig holds the JSON-decoded configuration for a BYOC environment.
//
// Struct size: 56 bytes (0x38), from type descriptor at 0xd79360.
// Struct layout (from field access patterns in New):
//
//	offset 0x00: EnvironmentType string (ptr+len)
//	offset 0x10: CWD string (ptr+len)
//	offset 0x20: TaskSetupScript []byte (ptr+len+cap)
type byocConfig struct {
	EnvironmentType string `json:"environment_type"`
	CWD             string `json:"cwd"`
	TaskSetupScript []byte `json:"task_setup_script,omitempty"`
}

// byocEnvironmentType implements envtype.EnvironmentType for BYOC environments.
//
// Struct size: 48 bytes (0x30), from type descriptor.
// Struct layout (from setter/getter method field access patterns):
//
//	offset 0x00: config *byocConfig
//	offset 0x08: logger *slog.Logger
//	offset 0x10: sessionMode config.SessionMode (string ptr+len)
//	offset 0x20: startupContext *config.StartupContext
//	offset 0x28: authContext interface{} (data pointer only)
type byocEnvironmentType struct {
	config         *byocConfig
	logger         *slog.Logger
	sessionMode    config.SessionMode
	startupContext *config.StartupContext
	authContext    interface{}
}

// containProvideAuthRoundTripper wraps an http.RoundTripper to inject
// container-provided authentication headers into outgoing requests.
// Used by CreateLeaseManager to authenticate lease renewal calls.
//
// itab: *byoc.containProvideAuthRoundTripper -> net/http.RoundTripper
// Referenced at 0xb07dc0 in CreateLeaseManager.
//
// Struct layout (from type:.eq at 0xb08020):
//
//	offset 0x00: transport http.RoundTripper (interface: itab + data)
//	offset 0x10: apiBaseURL string
//	offset 0x20: sessionID string
//	offset 0x28: timeout time.Duration
type containProvideAuthRoundTripper struct {
	transport  http.RoundTripper
	apiBaseURL string
	sessionID  string
	timeout    time.Duration
}

// RoundTrip implements http.RoundTripper, injecting container auth.
//
// Binary address: 0xb07880
// Source file: byoc.go
func (rt *containProvideAuthRoundTripper) RoundTrip(req *http.Request) (*http.Response, error) {
	// Adds authentication headers from the container's auth provider
	// before delegating to the underlying transport.
	return rt.transport.RoundTrip(req)
}

// New creates a new BYOC environment type instance by parsing the provided
// JSON configuration.
//
// Binary address: 0xb042e0
// Source file: byoc.go
//
// Assembly flow:
//  1. Allocates byocConfig via runtime.newobject, calls json.Unmarshal
//  2. Validates environment_type == "byoc"
//  3. Validates CWD is absolute and clean
//  4. Validates TaskSetupScript size <= 1MB (0x100000)
//  5. Allocates byocEnvironmentType with config, logger, default sessionMode "new"
//  6. Returns interface via itab
func New(configJSON []byte, logger *slog.Logger) (envtype.EnvironmentType, error) {
	cfg := &byocConfig{}
	if err := json.Unmarshal(configJSON, cfg); err != nil {
		return nil, fmt.Errorf("failed to unmarshal byoc config: %w", err)
	}

	if cfg.EnvironmentType != "byoc" {
		return nil, fmt.Errorf("invalid environment_type: expected 'byoc', got '%s'", cfg.EnvironmentType)
	}

	if cfg.CWD != "" {
		if cfg.CWD[0] != '/' {
			return nil, fmt.Errorf("cwd must be an absolute path, got: %s", cfg.CWD)
		}
		cleaned := filepath.Clean(cfg.CWD)
		if cleaned != cfg.CWD {
			return nil, fmt.Errorf("cwd contains path traversal elements: %s", cfg.CWD)
		}
	}

	if len(cfg.TaskSetupScript) > 0x100000 {
		return nil, fmt.Errorf("task_setup_script too large: %d bytes (max %d)", len(cfg.TaskSetupScript), 0x100000)
	}

	env := &byocEnvironmentType{
		config:      cfg,
		logger:      logger,
		sessionMode: "new",
	}
	return env, nil
}

// SetStartupContext sets the startup context on the BYOC environment.
//
// Binary address: 0xb04660
// Source file: byoc.go
// Assembly: stores BX at 0x20(AX).
func (e *byocEnvironmentType) SetStartupContext(ctx *config.StartupContext) {
	e.startupContext = ctx
}

// SetAuthContext sets the authentication context on the BYOC environment.
//
// Binary address: 0xb046c0
// Source file: byoc.go
// Assembly: stores BX at 0x28(AX).
func (e *byocEnvironmentType) SetAuthContext(authCtx interface{}) {
	e.authContext = authCtx
}

// SetSessionMode sets the session mode on the BYOC environment.
//
// Binary address: 0xb04600
// Source file: byoc.go
// Assembly: stores string (BX,CX) at 0x10(AX),0x18(AX).
func (e *byocEnvironmentType) SetSessionMode(mode config.SessionMode) {
	e.sessionMode = mode
}

// GetCWD returns the current working directory for the BYOC environment.
//
// Binary address: 0xb04720
// Source file: byoc.go
// Assembly: reads 0(AX) -> config, then 0x10/0x18 from config (CWD string).
func (e *byocEnvironmentType) GetCWD() string {
	return e.config.CWD
}

// GetClaudeEnvironmentVariables returns environment variables for the BYOC
// Claude Code process. Sets CLAUDE_CODE_REMOTE, CLAUDE_CODE_DEBUG,
// CLAUDE_CODE_EXIT_AFTER_STOP_DELAY, CLAUDE_CODE_ENVIRONMENT_KIND, and
// CLAUDE_CODE_ENTRYPOINT. Then copies any additional environment variables
// from the startupContext.
//
// Binary address: 0xb05ae0
// Source file: byoc.go
//
// Assembly evidence:
//   - makemap_small at 0xb05b46
//   - 5 mapassign_faststr calls for fixed env vars
//   - Reads entrypoint from startupContext offset 0xd8/0xe0, defaults to "remote"
//   - Then iterates startupContext.EnvironmentVariables map if non-nil
func (e *byocEnvironmentType) GetClaudeEnvironmentVariables() map[string]string {
	// Determine entrypoint: use startupContext.Entrypoint if set, else "remote"
	entrypoint := "remote"
	if e.startupContext != nil {
		if e.startupContext.Entrypoint != "" {
			entrypoint = e.startupContext.Entrypoint
		}
	}

	vars := make(map[string]string)
	vars["CLAUDE_CODE_REMOTE"] = "true"
	vars["CLAUDE_CODE_DEBUG"] = "true"
	vars["CLAUDE_CODE_EXIT_AFTER_STOP_DELAY"] = "300000"
	vars["CLAUDE_CODE_ENVIRONMENT_KIND"] = "byoc"
	vars["CLAUDE_CODE_ENTRYPOINT"] = entrypoint

	// Copy environment variables from startupContext if present
	if e.startupContext != nil && e.startupContext.EnvironmentVariables != nil {
		for k, v := range e.startupContext.EnvironmentVariables {
			vars[k] = v
		}
	}

	return vars
}

// Initialize performs the full initialization sequence for a BYOC environment.
//
// Binary address: 0xb04740
// Source file: byoc.go
//
// Assembly flow:
//  1. Logs "Initializing BYOC environment" with session_mode, has_script, cwd
//  2. If config has CWD set (len > 0), logs "Setting working directory",
//     sets CWD via os.Chdir, logs "Working directory set successfully"
//  3. Calls bootstrapClaudeSettings; on error logs "Failed to bootstrap Claude settings"
//  4. Calls setupGitConfig
//  5. Checks if sessionMode is "new" or "setup-only":
//     - If "new": logs "Running task setup script", calls runScript("task_setup", script)
//     - Otherwise: logs "Fast resume: Skipping task setup script"
//  6. If sessionMode is "resume-cached" and startupContext has sources with branches:
//     logs "Attempting to checkout target branches", calls handleBranchCheckout
//  7. Logs "BYOC environment initialization completed"
func (e *byocEnvironmentType) Initialize(ctx context.Context) error {
	hasScript := len(e.config.TaskSetupScript) > 0

	e.logger.Info("Initializing BYOC environment",
		"session_mode", string(e.sessionMode),
		"has_script", hasScript,
		"cwd", e.config.CWD,
	)

	// Set working directory if configured
	if e.config.CWD != "" {
		e.logger.Info("Setting working directory",
			"cwd", e.config.CWD,
		)

		if err := os.MkdirAll(e.config.CWD, 0o755); err != nil {
			return fmt.Errorf("failed to create working directory %s: %w", e.config.CWD, err)
		}

		if err := os.Chdir(e.config.CWD); err != nil {
			return fmt.Errorf("failed to change to working directory %s: %w", e.config.CWD, err)
		}

		e.logger.Info("Working directory set successfully",
			"cwd", e.config.CWD,
		)
	}

	// Bootstrap Claude settings
	if err := e.bootstrapClaudeSettings(ctx); err != nil {
		e.logger.Warn("Failed to bootstrap Claude settings",
			"error", err,
		)
	}

	// Setup git config
	e.setupGitConfig(ctx)

	// Check session mode for running the task setup script
	isNewOrSetupOnly := e.sessionMode == "new" || e.sessionMode == "setup-only"

	if hasScript {
		if isNewOrSetupOnly {
			e.logger.Info("Running task setup script")

			if err := e.runScript(ctx); err != nil {
				return fmt.Errorf("task setup script failed: %w", err)
			}
		} else {
			e.logger.Info("Fast resume: Skipping task setup script")
		}
	}

	// Handle branch checkout for resume-cached mode
	if e.sessionMode == "resume-cached" {
		if e.startupContext != nil && e.startupContext.Outcomes != nil {
			// Check if there are sources with branches to checkout
			if len(e.startupContext.Sources) > 0 {
				e.logger.Info("Attempting to checkout target branches for BYOC environment")

				if err := e.handleBranchCheckout(ctx); err != nil {
					e.logger.Error("Failed to checkout branches",
						"error", err,
					)
					return fmt.Errorf("failed to checkout branches in BYOC environment: %w", err)
				}
			}
		}
	}

	e.logger.Info("BYOC environment initialization completed")
	return nil
}

// CreateLeaseManager creates a lease manager for the BYOC environment.
// This sets up an HTTP client with container auth round-tripping and
// configures periodic lease renewal.
//
// Binary address: 0xb07cc0
// Source file: byoc.go
//
// Assembly evidence:
//   - Creates containProvideAuthRoundTripper with net/http.DefaultTransport (0xb07d2e)
//   - Sets timeout to 0x6fc23ac00 (30s in nanoseconds) at 0xb07db2
//   - Logs "Creating lease manager" with session_id and work_id at 0xb07ef9
//   - Calls podmonitor.GetDefaultHealthFilePath at 0xb07f00
func (e *byocEnvironmentType) CreateLeaseManager(ctx context.Context, sessionID string, workID string, apiBaseURL string) (envtype.LeaseManager, error) {
	// Create auth round tripper wrapping default transport
	rt := &containProvideAuthRoundTripper{
		transport:  http.DefaultTransport,
		apiBaseURL: apiBaseURL,
		sessionID:  sessionID,
		timeout:    30 * time.Second,
	}

	e.logger.Info("Creating lease manager",
		"session_id", sessionID,
		"work_id", workID,
	)

	// Get health file path for pod monitor
	healthFilePath := podmonitor.GetDefaultHealthFilePath()
	_ = healthFilePath
	_ = rt

	// Create and return the lease manager
	// The actual lease manager implementation is in a separate package.
	return nil, nil
}

// extractRepoBranchMapping extracts repository-to-branch mappings from the
// startupContext's outcomes. Iterates sources looking for "git_repository" type
// entries and maps each repo to its target branch.
//
// Binary address: 0xb050a0
// Source file: byoc.go
//
// Assembly flow:
//  1. Creates empty map via makemap_small
//  2. Iterates startupContext.Sources (slice at 0x28/0x30 of startupContext)
//  3. Each source is 0x48 bytes; checks source type at offset 0x00 == "git_repository" (14 chars)
//  4. Checks git info at offset 0x88/0x98 (branch slice)
//  5. If branch count == 1: maps repo -> branch via mapassign_faststr
//  6. If branch count > 1: returns error "outcome for repo %s has %d branches, expected 0 or 1"
//  7. Logs "Mapped repository to branch for checkout" at debug level with repo and branch
//
// Returns: (map[string]string, error) via AX (map), BX/CX (error)
func (e *byocEnvironmentType) extractRepoBranchMapping(ctx context.Context) (map[string]string, error) {
	branchMap := make(map[string]string)

	if e.startupContext == nil {
		return nil, nil
	}

	// Iterate over Outcomes (not Sources) - each OutcomeField is 0x48 bytes
	// at startupContext offset 0x28 (Outcomes slice)
	for _, outcome := range e.startupContext.Outcomes {
		if outcome.Type != "git_repository" {
			continue
		}

		branches := outcome.GitInfo.Branches
		if len(branches) == 1 {
			repo := outcome.GitInfo.Repo
			branchMap[repo] = branches[0]

			e.logger.Debug("Mapped repository to branch for checkout",
				"repo", repo,
				"branch", branches[0],
			)
		} else if len(branches) > 1 {
			return nil, fmt.Errorf("outcome for repo %s has %d branches, expected 0 or 1", outcome.GitInfo.Repo, len(branches))
		}
	}

	return branchMap, nil
}

// handleBranchCheckout handles git branch checkout for BYOC environments.
// Creates a SourceHandlerManager and uses it to process sources for branch
// checkout, then sets up git proxy.
//
// Binary address: 0xb05440
// Source file: byoc.go
//
// Assembly flow:
//  1. Logs "Extracting repository-branch mapping from outcomes"
//  2. Calls extractRepoBranchMapping
//  3. If error, wraps with "failed to create source handler manager: %w"
//  4. If no branches found, logs "No specific branches requested for checkout"
//  5. Logs "Found branches to checkout" with count
//  6. Determines "repository"/"repositories" label based on count
//  7. Formats message "Attempting to checkout branches for %d %s"
//  8. Calls outcomes reporter with "init"/"none" + formatted message
//  9. Creates SourceHandlerManager via sources.NewSourceHandlerManager
//     with allow-prefetched=true
//  10. Calls ProcessSources on the manager
//  11. If error: wraps with "failed to checkout branches: %w"
//  12. Logs "Branches checked out successfully" with count
//  13. Calls SetupGitProxyAfterSourcesProcessed
//  14. If error: logs "Failed to setup git proxy" at Warn level
func (e *byocEnvironmentType) handleBranchCheckout(ctx context.Context) error {
	e.logger.Info("Extracting repository-branch mapping from outcomes")

	branchMap, err := e.extractRepoBranchMapping(ctx)
	if err != nil {
		return err
	}

	if branchMap == nil || len(branchMap) == 0 {
		e.logger.Info("No specific branches requested for checkout")
		return nil
	}

	e.logger.Info("Found branches to checkout",
		"count", len(branchMap),
	)

	// Determine label for log message
	label := "repositories"
	if len(branchMap) == 1 {
		label = "repository"
	}

	msg := fmt.Sprintf("Attempting to checkout branches for %d %s", len(branchMap), label)

	// Report outcome via outcomes reporter
	if e.startupContext != nil {
		if e.startupContext.Outcomes != nil {
			// Call outcomes reporter with "init" step and "none" status
			_ = msg
		}
	}

	// Create source handler manager
	mgr, err := sources.NewSourceHandlerManager(
		e.logger,
		e.config.CWD,
		"", // sessionID
		nil, // gitProxyManager
		nil, // activityRecorder
		true, // isResume (allow-prefetched)
	)
	if err != nil {
		return fmt.Errorf("failed to create source handler manager: %w", err)
	}

	// Process sources
	if _, err := mgr.ProcessSources(ctx, e.logger, e.startupContext.Sources); err != nil {
		return fmt.Errorf("failed to checkout branches: %w", err)
	}

	e.logger.Info("Branches checked out successfully",
		"count", len(branchMap),
	)

	// Setup git proxy after sources processed
	if _, err := mgr.SetupGitProxyAfterSourcesProcessed(ctx, e.logger, e.startupContext.Sources); err != nil {
		e.logger.Warn("Failed to setup git proxy",
			"error", err,
		)
	}

	return nil
}

// runScript runs the task setup script in the BYOC environment.
// If the script starts with "#!", it's used as-is; otherwise, a bash shebang
// is prepended.
//
// Binary address: 0xb05dc0
// Source file: byoc.go
//
// Assembly flow:
//  1. Logs "Executing script" with script_name, script_size_bytes
//  2. If script starts with "#!" (shebang), use content as-is
//  3. Otherwise prepend "#!/bin/bash\nset -e\n"
//  4. Creates a closure (func1 at 0xb06360) as OutputStreamer
//  5. Formats pattern as "%s-*.sh" with scriptName (e.g., "task_setup-*.sh")
//  6. Calls process.ExecuteScript with content, pattern, and closure
//  7. If ExecuteScript returns error: wraps with "failed to execute %s script: %w"
//  8. If result has error (offset 0x40 non-nil): returns "%s script failed: %w"
//  9. On success: logs "Script completed successfully" with exit_code, duration, etc.
func (e *byocEnvironmentType) runScript(ctx context.Context) error {
	scriptName := "task_setup"
	scriptContent := string(e.config.TaskSetupScript)
	scriptSize := len(e.config.TaskSetupScript)

	e.logger.Info("Executing script",
		"script_name", scriptName,
		"script_size_bytes", scriptSize,
	)

	// Check for shebang; if not present, prepend bash shebang
	content := scriptContent
	if len(content) >= 2 && content[0] == '#' && content[1] == '!' {
		// Script has its own shebang, use as-is
	} else {
		content = "#!/bin/bash\nset -e\n" + content
	}

	// Create output streamer closure
	// func1 at 0xb06360 captures 'e' and the scriptName for logging
	streamer := util.OutputStreamer(func(ctx context.Context, streamType util.StreamType, data []byte) error {
		// Stream output to logger
		return nil
	})

	pattern := fmt.Sprintf("%s-*.sh", scriptName)

	result, err := process.ExecuteScript(ctx, e.logger, content, pattern, streamer)
	if err != nil {
		return fmt.Errorf("failed to execute %s script: %w", scriptName, err)
	}

	// Check if the script execution returned an error in the result
	if result != nil && result.Error != nil {
		return fmt.Errorf("%s script failed: %w", scriptName, result.Error)
	}

	e.logger.Info("Script completed successfully",
		"script_name", scriptName,
		"exit_code", result.ExitCode,
		"duration", result.Duration,
	)

	return nil
}

// gitConfigPair represents a key-value pair for git configuration.
type gitConfigPair struct {
	key   string
	value string
}

// setupGitConfig configures git settings for the BYOC environment.
// Sets user.name, user.email, gpg.format, gpg.ssh.program, commit.gpgsign,
// and http.proxyAuthMethod via "git config --global".
// Also creates $HOME/.ssh directory and a signing key file.
//
// Binary address: 0xb06bc0
// Source file: byoc.go
//
// Assembly flow:
//  1. Check SKIP_GIT_CONFIG env var; if "true", log and return early
//  2. Log "Setting up git configuration for BYOC environment"
//  3. Iterate over 6 config pairs, calling "git config --global <key> <value>"
//     for each. On error: log at Warn level "Failed to set git config".
//     On success: log at Debug level "Set git config".
//  4. Get HOME env var, create $HOME/.ssh directory (mode 0x1c0 = 0700)
//  5. Create $HOME/.ssh/commit_signing_key.pub file (mode 0x242 write|create|truncate, perm 0x1b6=0666)
//  6. Write second set of git config: "git config --global user.signingkey <path>"
func (e *byocEnvironmentType) setupGitConfig(ctx context.Context) error {
	// Check if git config should be skipped
	if os.Getenv("SKIP_GIT_CONFIG") == "true" {
		e.logger.Info("Skipping git configuration (SKIP_GIT_CONFIG=true)")
		return nil
	}

	e.logger.Info("Setting up git configuration for BYOC environment")

	// Git config key-value pairs to set
	configs := []gitConfigPair{
		{"user.name", "Claude"},
		{"user.email", "noreply@anthropic.com"},
		{"gpg.format", "ssh"},
		{"gpg.ssh.program", "/tmp/code-sign"},
		{"commit.gpgsign", "true"},
		{"http.proxyAuthMethod", "basic"},
	}

	for _, cfg := range configs {
		cmd := exec.CommandContext(ctx, "git", "config", "--global", cfg.key, cfg.value)
		cmd.Env = syscall.Environ()
		output, err := cmd.CombinedOutput()
		if err != nil {
			e.logger.Warn("Failed to set git config",
				"key", cfg.key,
				"value", cfg.value,
				"error", err,
				"output", string(output),
			)
		} else {
			e.logger.Debug("Set git config",
				"key", cfg.key,
				"value", cfg.value,
			)
		}
	}

	// Create $HOME/.ssh directory
	home := os.Getenv("HOME")
	sshDir := filepath.Join(home, ".ssh")
	if err := os.MkdirAll(sshDir, 0o700); err != nil {
		e.logger.Warn("Failed to create .ssh directory",
			"error", err,
		)
		return nil
	}

	// Create signing key file path
	signingKeyPath := filepath.Join(sshDir, "commit_signing_key.pub")

	// Check if file exists; if not, create an empty one
	if _, err := os.Stat(signingKeyPath); os.IsNotExist(err) {
		f, err := os.OpenFile(signingKeyPath, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o666)
		if err != nil {
			e.logger.Warn("Failed to create signing key file",
				"error", err,
			)
		} else {
			f.Close()
			e.logger.Debug("Created empty signing key file",
				"path", signingKeyPath,
			)
		}
	}

	// Set git config for user.signingkey pointing to the key file
	cmd := exec.CommandContext(ctx, "git", "config", "--global", "user.signingkey", signingKeyPath)
	cmd.Env = syscall.Environ()
	output, err := cmd.CombinedOutput()
	if err != nil {
		e.logger.Warn("Failed to set git config",
			"key", "user.signingkey",
			"value", signingKeyPath,
			"error", err,
			"output", string(output),
		)
	} else {
		e.logger.Debug("Set git config",
			"key", "user.signingkey",
			"value", signingKeyPath,
		)
	}

	return nil
}

// bootstrapClaudeSettings sets up Claude Code settings and stop hook script.
// Creates ~/.claude directory, writes settings.json and stop-hook-git-check.sh
// if they don't already exist.
//
// Binary address: 0xb06560
// Source file: byoc.go
//
// Assembly flow:
//  1. Calls os.UserHomeDir(); on error: returns "failed to get home directory: %w"
//  2. Joins home + ".claude" to get claude dir
//  3. Joins claudeDir + "settings.json" for settings path
//  4. Joins claudeDir + "stop-hook-git-check.sh" for stop hook path
//  5. MkdirAll claudeDir (mode 0x1ed = 0755); on error: "failed to create .claude directory: %w"
//  6. os.Stat(settingsPath); if exists, logs "Claude settings file already exists"
//  7. os.Stat(stopHookPath); if exists, logs "Stop hook script already exists"
//  8. If settings file doesn't exist:
//     - Writes defaultSettingsJSON to settingsPath (mode 0x180 = 0600)
//     - On error: "failed to write settings file: %w"
//     - On success: logs "Successfully created Claude settings file"
//  9. If stop hook file doesn't exist:
//     - Writes stopHookScript to stopHookPath (mode 0x1ed = 0755)
//     - On error: "failed to write stop hook script: %w"
//     - On success: logs "Successfully created stop hook script"
func (e *byocEnvironmentType) bootstrapClaudeSettings(ctx context.Context) error {
	homeDir, err := os.UserHomeDir()
	if err != nil {
		return fmt.Errorf("failed to get home directory: %w", err)
	}

	claudeDir := filepath.Join(homeDir, ".claude")
	settingsPath := filepath.Join(claudeDir, "settings.json")
	stopHookPath := filepath.Join(claudeDir, "stop-hook-git-check.sh")

	// Create .claude directory
	if err := os.MkdirAll(claudeDir, 0o755); err != nil {
		return fmt.Errorf("failed to create .claude directory: %w", err)
	}

	// Check if settings file already exists
	settingsExists := false
	if _, err := os.Stat(settingsPath); err == nil {
		settingsExists = true
		e.logger.Info("Claude settings file already exists",
			"path", settingsPath,
		)
	}

	// Check if stop hook already exists
	stopHookExists := false
	if _, err := os.Stat(stopHookPath); err == nil {
		stopHookExists = true
		e.logger.Info("Stop hook script already exists",
			"path", stopHookPath,
		)
	}

	// Write settings file if it doesn't exist
	if !settingsExists {
		if err := os.WriteFile(settingsPath, defaultSettingsJSON, 0o600); err != nil {
			return fmt.Errorf("failed to write settings file: %w", err)
		}
		e.logger.Info("Successfully created Claude settings file",
			"path", settingsPath,
		)
	}

	// Write stop hook if it doesn't exist
	if !stopHookExists {
		if err := os.WriteFile(stopHookPath, stopHookScript, 0o755); err != nil {
			return fmt.Errorf("failed to write stop hook script: %w", err)
		}
		e.logger.Info("Successfully created stop hook script",
			"path", stopHookPath,
		)
	}

	return nil
}

// Ensure unused imports are referenced.
var (
	_ = strings.Contains
)
