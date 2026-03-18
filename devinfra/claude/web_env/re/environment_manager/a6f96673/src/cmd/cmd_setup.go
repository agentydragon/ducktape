// Reconstructed from binary: environment-manager (Build ID a6f96673)
// Source: cmd/cmd_setup.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/cmd

package cmd

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"os/exec"
	"strings"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/claude"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/logger"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/orchestrator"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/sandbox"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/session"
	"github.com/spf13/cobra"
)

// AddSetupCommand adds the "setup" subcommand to the root cobra command.
// The setup command initializes an environment by installing Claude Code
// and sandbox-runtime, with optional API healthcheck.
//
// Binary: 0xb76f00 - cmd.AddSetupCommand
// Source: cmd/cmd_setup.go
//
// Parameters:
//
//	AX = *cobra.Command (parent/root command)
//
// Flags registered (from pflag.(*FlagSet).StringVar/BoolVarP calls):
//
//	--log-level              (StringVar, default "info")   "Log level (debug, info, warn, error)"
//	--claude-code-version    (StringVar, default "latest") "Version of Claude Code to install..."
//	--sandbox-runtime-version (StringVar, default "latest") "Version of sandbox-runtime to install..."
//	--skip-claude-code       (BoolVarP, default false)     "Skip Claude Code installation"
//	--skip-sandbox-runtime   (BoolVarP, default false)     "Skip sandbox-runtime installation"
//	--api-url                (StringVar, default "https://api.anthropic.com") "API base URL..."
//	--service-key-file       (StringVar, default "")       "Path to environment service key file..."
func AddSetupCommand(rootCmd *cobra.Command) {
	// Allocate flag variables.
	// Binary: 0xb76f22-0xb76f93 - multiple runtime.newobject calls
	var logLevel string              // offset 0x78
	var claudeCodeVersion string     // offset 0x80
	var sandboxRuntimeVersion string // offset 0x70
	var skipClaudeCode bool          // offset 0x50 (byte 0 of 2-byte alloc)
	var skipSandboxRuntime bool      // offset 0x50+1 (byte 1, separate pointer via LEA 0x1)
	var apiURL string                // offset 0x88
	var serviceKeyFile string        // offset 0x68

	// Create the cobra.Command.
	// Binary: 0xb76f98-0xb76fa5 runtime.newobject
	// Command struct fields:
	//   Use (offset 0x00+0x08): "setup" (0x05=5 chars)
	//   Short (offset 0x40+0x48): 0x2a=42 chars description
	//   Long (offset 0x60+0x68): 0x495=1173 chars extended description
	//   Example (offset 0x70+0x78): 0x23c=572 chars usage examples
	setupCmd := &cobra.Command{
		Use:   "setup",
		Short: "Install dependencies for orchestrator mode",
		Long: `The setup command pre-installs all required dependencies for orchestrator mode.

This command is intended to be run during container image build to ensure that
all dependencies are available when the worker starts. It installs:

  - Claude Code (@anthropic-ai/claude-code) via npm
  - Sandbox Runtime (@anthropic-ai/sandbox-runtime) via npm

Running setup does NOT disable auto-updates. The orchestrator and task-run
commands still check for updates on each session. The benefit of running setup
is faster first session startup since dependencies are pre-installed.

Prerequisites:
  - npm must be available in PATH (comes with Node.js)
  - Node.js must be installed (for running the installed packages)

Optionally, provide an environment service key to verify API connectivity:
  --service-key-file /path/to/key    Verify environment can reach the API

The healthcheck calls /v1/environments/whoami to validate the service key
and confirm network connectivity. Recommended for manual testing.

Note: Language runtimes (Node.js, Python, Go) must be pre-installed in the
container image. The environment manager only creates symlinks to these during
session initialization.`,
		Example: `  # Basic usage - install everything with defaults (no healthcheck)
  environment-runner setup

  # Specify versions
  environment-runner setup --claude-code-version 2.0.20 --sandbox-runtime-version 1.2.3

  # Skip certain installations
  environment-runner setup --skip-sandbox-runtime

  # Setup with healthcheck - for manual testing
  environment-runner setup --service-key-file /path/to/key

  # Setup with healthcheck via env var
  export ENVIRONMENT_SERVICE_KEY="sk-ant-..."
  environment-runner setup

  # Verbose output
  environment-runner setup --log-level debug`,
		// Binary: 0xb772a0 - AddSetupCommand.func1
		RunE: func(cmd *cobra.Command, args []string) error {
			return runSetup(cmd.Context(), logLevel, claudeCodeVersion, sandboxRuntimeVersion, skipClaudeCode, skipSandboxRuntime, apiURL, serviceKeyFile)
		},
	}

	// Register flags.
	// Binary: 0xb770cd-0xb77253 - series of pflag calls
	setupCmd.Flags().StringVar(&logLevel, "log-level", "info", "Log level (debug, info, warn, error)")
	setupCmd.Flags().StringVar(&claudeCodeVersion, "claude-code-version", "latest", "Version of Claude Code to install (latest, stable, or specific version like 2.0.20)")
	setupCmd.Flags().StringVar(&sandboxRuntimeVersion, "sandbox-runtime-version", "latest", "Version of sandbox-runtime to install (latest or specific version)")
	setupCmd.Flags().BoolVar(&skipClaudeCode, "skip-claude-code", false, "Skip Claude Code installation")
	setupCmd.Flags().BoolVar(&skipSandboxRuntime, "skip-sandbox-runtime", false, "Skip sandbox-runtime installation")
	setupCmd.Flags().StringVar(&apiURL, "api-url", "https://api.anthropic.com", "API base URL for connectivity healthcheck")
	setupCmd.Flags().StringVar(&serviceKeyFile, "service-key-file", "", "Path to environment service key file for API healthcheck. Falls back to ENVIRONMENT_SERVICE_KEY env var if not set. When provided, verifies connectivity by calling /v1/environments/whoami")

	// Add setup command to root.
	// Binary: 0xb77277 cobra.(*Command).AddCommand
	rootCmd.AddCommand(setupCmd)
}

// runSetup is the main execution function for the setup command.
// It parses the log level, creates a logger, runs preflight checks,
// loads the service key, optionally runs API healthcheck, installs
// Claude Code and sandbox-runtime, then logs completion.
//
// Binary: 0xb77720 - cmd.runSetup
// Source: cmd/cmd_setup.go
//
// Parameters (from closure in AddSetupCommand.func1 at 0xb772a0):
//
//	AX/BX = ctx (context.Context interface)
//	CX/DI = logLevel (string)
//	SI/R8 = claudeCodeVersion (string)
//	R9/R10 = sandboxRuntimeVersion (string)
//	R11 = skipClaudeCode (bool)
//	stack 0(SP) = skipSandboxRuntime (bool)
//	stack 0x8(SP)/0x10(SP) = apiURL (string)
//	stack 0x18(SP)/0x20(SP) = serviceKeyFile (string)
//
// Flow:
//  1. parseLogLevel(logLevel) -> error if invalid
//  2. CreateLoggerWithFileOutput(level) -> logger
//  3. Log "Starting setup" with 4 attrs (versions + skip flags)
//  4. If !(skipClaudeCode && skipSandboxRuntime): runPreflightChecks
//  5. loadServiceKey(serviceKeyFile)
//  6. If serviceKey non-empty: runAPIHealthcheck; if error -> return
//     If serviceKey empty: log warning
//  7. If !skipClaudeCode: installClaudeCode; else log skip message
//  8. If !skipSandboxRuntime: installSandboxRuntime; else log skip message
//  9. Log "Setup complete..." and return nil
func runSetup(
	ctx context.Context,
	logLevel string,
	claudeCodeVersion string,
	sandboxRuntimeVersion string,
	skipClaudeCode bool,
	skipSandboxRuntime bool,
	apiURL string,
	serviceKeyFile string,
) error {
	// Binary: 0xb77783 - parseLogLevel
	level, err := parseLogLevel(logLevel)
	if err != nil {
		// Binary: 0xb77bfc - return parseLogLevel error
		return err
	}

	// Binary: 0xb777a2 - CreateLoggerWithFileOutput
	log := logger.CreateLoggerWithFileOutput(level)

	// Binary: 0xb778d6-0xb779a8 - slog.Info with 4 attrs
	log.Info("Starting setup",
		"claude_code_version", claudeCodeVersion,
		"sandbox_runtime_version", sandboxRuntimeVersion,
		"skip_claude_code", skipClaudeCode,
		"skip_sandbox_runtime", skipSandboxRuntime,
	)

	// Binary: 0xb779ad-0xb779c3 - conditional preflight
	// Skip preflight checks only if BOTH installs are skipped (no npm needed).
	if !(skipClaudeCode && skipSandboxRuntime) {
		// Binary: 0xb779e0 - runPreflightChecks
		if err := runPreflightChecks(ctx, log); err != nil {
			// Binary: 0xb77bf3 - return preflight error
			return err
		}
	}

	// Binary: 0xb77a00 - loadServiceKey
	serviceKey, err := loadServiceKey(serviceKeyFile)
	if err != nil {
		// Binary: 0xb77be4 - return loadServiceKey error
		return err
	}

	// Binary: 0xb77a0e-0xb77a60 - conditional API healthcheck
	if serviceKey != "" {
		// Binary: 0xb77a41 - runAPIHealthcheck
		if err := runAPIHealthcheck(ctx, log, apiURL, serviceKey); err != nil {
			// Binary: 0xb77a57 - return healthcheck error
			return err
		}
	} else {
		// Binary: 0xb77a7c - slog.Warn
		log.Warn("Skipping API healthcheck (no service key provided)")
	}

	// Binary: 0xb77a97-0xb77b08 - conditional Claude Code install
	if skipClaudeCode {
		// Binary: 0xb77abe - slog.Warn
		log.Warn("Skipping Claude Code installation (--skip-claude-code)")
	} else {
		// Binary: 0xb77b03 - installClaudeCode
		if err := installClaudeCode(ctx, log, claudeCodeVersion); err != nil {
			// Binary: 0xb77bdb - return install error
			return err
		}
	}

	// Binary: 0xb77b11-0xb77b80 - conditional sandbox-runtime install
	if skipSandboxRuntime {
		// Binary: 0xb77b37 - slog.Warn
		log.Warn("Skipping sandbox-runtime installation (--skip-sandbox-runtime)")
	} else {
		// Binary: 0xb77b80 - installSandboxRuntime
		if err := installSandboxRuntime(ctx, log, sandboxRuntimeVersion); err != nil {
			// Binary: 0xb77bd2 - return install error
			return err
		}
	}

	// Binary: 0xb77ba5 - slog.Info
	log.Info("Setup complete. Worker image is ready for orchestrator mode.")

	// Binary: 0xb77bc5-0xb77bd1 - return nil
	return nil
}

// loadServiceKey reads the service key from the given path or falls back
// to the ENVIRONMENT_SERVICE_KEY environment variable.
//
// Binary: 0xb77380 - cmd.loadServiceKey
// Source: cmd/cmd_setup.go
//
// Parameters:
//
//	AX = secretPath string data pointer
//	BX = secretPath string length
//
// Returns:
//
//	AX = service key string data pointer
//	BX = service key string length
//	CX = error interface type (0 if nil)
//	DI = error interface data (0 if nil)
//
// Flow:
//  1. If secretPath is empty (BX==0): jump to os.Getenv fallback
//  2. Call os.ReadFile(secretPath)
//  3. If error: return fmt.Errorf("failed to read service key file %q: %w", secretPath, err)
//  4. Convert bytes to string, call strings.TrimSpace
//  5. If result is empty: fall through to env var fallback
//  6. Fallback: return os.Getenv("ENVIRONMENT_SERVICE_KEY"), nil
func loadServiceKey(secretPath string) (string, error) {
	// Binary: 0xb77397-0xb773a0 - check empty path
	if secretPath == "" {
		// Binary: 0xb77460-0xb77471 - empty result, fall through to Getenv
		return os.Getenv("ENVIRONMENT_SERVICE_KEY"), nil
	}

	// Binary: 0xb773af - os.ReadFile
	data, err := os.ReadFile(secretPath)
	if err != nil {
		// Binary: 0xb773bd-0xb77436 - fmt.Errorf with path and error
		return "", fmt.Errorf("failed to read service key file %q: %w", secretPath, err)
	}

	// Binary: 0xb7744b-0xb77458 - slicebytetostring + TrimSpace
	key := strings.TrimSpace(string(data))

	// Binary: 0xb77460-0xb77471 - if key is empty, fallback to env var
	if key == "" {
		return os.Getenv("ENVIRONMENT_SERVICE_KEY"), nil
	}

	// Binary: 0xb77476-0xb77480 - return key, nil
	return key, nil
}

// runAPIHealthcheck verifies connectivity to the API by calling the
// /v1/environments/whoami endpoint via the WhoamiClient.
//
// Binary: 0xb774a0 - cmd.runAPIHealthcheck
// Source: cmd/cmd_setup.go
//
// Parameters:
//
//	AX/BX = ctx (context.Context)
//	CX = *slog.Logger
//	DI/SI = apiURL (string)
//	R8/R9 = serviceKey (string)
//
// Flow:
//  1. Log "Running API connectivity healthcheck"
//  2. Call orchestrator.NewWhoamiClient(apiURL, serviceKey, logger)
//  3. Call client.GetIdentity(ctx)
//  4. If error: return fmt.Errorf("API connectivity healthcheck failed: %w", err)
//  5. Log identity info with "environment_id" and "organization_uuid" attrs
//  6. Log "API connectivity verified"
//  7. Return nil
func runAPIHealthcheck(ctx context.Context, log *slog.Logger, apiURL string, serviceKey string) error {
	// Binary: 0xb774f3-0xb7751a - slog.Info
	log.Info("Running API connectivity healthcheck")

	// Binary: 0xb77547 - NewWhoamiClient
	client := orchestrator.NewWhoamiClient(apiURL, serviceKey, "", log)

	// Binary: 0xb77560 - GetIdentity
	identity, err := client.GetIdentity(ctx)
	if err != nil {
		// Binary: 0xb7756a-0xb775a5 - fmt.Errorf
		return fmt.Errorf("API connectivity healthcheck failed: %w", err)
	}

	// Binary: 0xb775b3-0xb776b4 - slog.Info with identity fields
	log.Info("API connectivity verified",
		"environment_id", identity.SessionID,
		"organization_uuid", identity.OrgID,
	)

	// Binary: 0xb776b9-0xb776c5 - return nil
	return nil
}

// runPreflightChecks performs environment validation before setup.
// Currently checks that npm is available (required for Claude Code installation).
//
// Binary: 0xb77c80 - cmd.runPreflightChecks
// Source: cmd/cmd_setup.go
//
// Parameters:
//
//	AX/BX = ctx (context.Context)
//	CX = *slog.Logger
//
// Flow:
//  1. Log "Running pre-flight checks"
//  2. Call checkNpmAvailable(ctx, logger)
//  3. If error: return error
//  4. Log "Pre-flight checks passed"
//  5. Return nil
func runPreflightChecks(ctx context.Context, log *slog.Logger) error {
	// Binary: 0xb77ca2-0xb77cc9 - slog.Info
	log.Info("Running pre-flight checks")

	// Binary: 0xb77ce0 - checkNpmAvailable
	if err := checkNpmAvailable(ctx, log); err != nil {
		// Binary: 0xb77cea-0xb77cef - return error
		return err
	}

	// Binary: 0xb77d00-0xb77d18 - slog.Info
	log.Info("Pre-flight checks passed")

	// Binary: 0xb77d1d-0xb77d26 - return nil
	return nil
}

// checkNpmAvailable verifies that npm is available on the PATH by running
// "npm --version". Returns a detailed error message if npm is not found.
//
// Binary: 0xb77d60 - cmd.checkNpmAvailable
// Source: cmd/cmd_setup.go
//
// Parameters:
//
//	AX/BX = ctx (context.Context)
//	CX = *slog.Logger
//
// Flow:
//  1. Log debug "Checking npm availability"
//  2. Run exec.CommandContext(ctx, "npm", "--version").Output()
//  3. If error: return fmt.Errorf with detailed npm-not-found message
//  4. Log debug "npm is available" with "version" attr
//  5. Return nil
func checkNpmAvailable(ctx context.Context, log *slog.Logger) error {
	// Binary: 0xb77d93-0xb77dc0 - slog.Debug (level -4)
	log.Debug("Checking npm availability")

	// Binary: 0xb77de0-0xb77e12 - CommandContext + Output
	output, err := exec.CommandContext(ctx, "npm", "--version").Output()
	if err != nil {
		// Binary: 0xb77e1c-0xb77e57 - fmt.Errorf with long message (0xb8=184 chars)
		return fmt.Errorf("pre-flight check failed: npm not found in PATH\n\nnpm is required to install Claude Code and sandbox-runtime.\nPlease install Node.js (which includes npm) before running setup.\n\nError: %w", err)
	}

	// Binary: 0xb77e65-0xb77e72 - slicebytetostring
	version := strings.TrimSpace(string(output))

	// Binary: 0xb77ee4-0xb77f00 - slog.Debug (level -4) with "version" attr
	log.Debug("npm is available", "version", version)

	// Binary: 0xb77f05-0xb77f11 - return nil
	return nil
}

// installClaudeCode installs or updates the Claude Code CLI tool.
// It checks the CLAUDE_DEFAULT_PATH env var for the install path
// (defaults to "claude"), then calls claude.InstallOrUpdateClaudeCode.
//
// Binary: 0xb77f40 - cmd.installClaudeCode
// Source: cmd/cmd_setup.go
//
// Parameters:
//
//	AX/BX = ctx (context.Context)
//	CX = *slog.Logger
//	DI/SI = claudeCodeVersion (string)
//
// Flow:
//  1. Log "Installing Claude Code" with "version" attr
//  2. Get CLAUDE_DEFAULT_PATH env var; if empty, default to "claude"
//  3. Call claude.InstallOrUpdateClaudeCode(ctx, version, defaultPath, &noopActivityRecorder{})
//  4. If error: return fmt.Errorf("failed to install Claude Code: %w", err)
//  5. Log "Claude Code installation completed"
//  6. Return nil
func installClaudeCode(ctx context.Context, log *slog.Logger, claudeCodeVersion string) error {
	// Binary: 0xb77f82-0xb7800b - slog.Info with "version" attr
	log.Info("Installing Claude Code", "version", claudeCodeVersion)

	// Binary: 0xb78010-0xb78038 - os.Getenv + CMOVE default
	claudeDefaultPath := os.Getenv("CLAUDE_DEFAULT_PATH")
	if claudeDefaultPath == "" {
		claudeDefaultPath = "claude"
	}

	// Binary: 0xb78078 - claude.InstallOrUpdateClaudeCode
	// Uses session.NoopActivityRecorder (itab: go:itab.*session.NoopActivityRecorder,session.ActivityRecorder)
	_, err := claude.InstallOrUpdateClaudeCode(log, ctx, claudeCodeVersion, claudeDefaultPath, &session.NoopActivityRecorder{}, nil)
	if err != nil {
		// Binary: 0xb78085-0xb780c0 - fmt.Errorf
		return fmt.Errorf("failed to install Claude Code: %w", err)
	}

	// Binary: 0xb780cf-0xb78100 - slog.Info
	log.Info("Claude Code installation completed")

	// Binary: 0xb78105-0xb78111 - return nil
	return nil
}

// installSandboxRuntime installs the sandbox runtime for secure execution.
// Delegates to sandbox.InstallSandboxRuntime from the internal sandbox package.
//
// Binary: 0xb78160 - cmd.installSandboxRuntime
// Source: cmd/cmd_setup.go
//
// Parameters:
//
//	AX/BX = ctx (context.Context)
//	CX = *slog.Logger
//	DI/SI = sandboxRuntimeVersion (string)
//
// Flow:
//  1. Log "Installing sandbox-runtime" with "version" attr
//  2. Call sandbox.InstallSandboxRuntime(ctx, version, logger)
//  3. If error: return fmt.Errorf("failed to install sandbox-runtime: %w", err)
//  4. Log "sandbox-runtime installation completed"
//  5. Return nil
func installSandboxRuntime(ctx context.Context, log *slog.Logger, sandboxRuntimeVersion string) error {
	// Binary: 0xb781a2-0xb7822b - slog.Info with "version" attr
	log.Info("Installing sandbox-runtime", "version", sandboxRuntimeVersion)

	// Binary: 0xb78258 - sandbox.InstallSandboxRuntime
	err := sandbox.InstallSandboxRuntime(log, ctx, sandboxRuntimeVersion)
	if err != nil {
		// Binary: 0xb78265-0xb782a0 - fmt.Errorf
		return fmt.Errorf("failed to install sandbox-runtime: %w", err)
	}

	// Binary: 0xb782af-0xb782e0 - slog.Info
	log.Info("sandbox-runtime installation completed")

	// Binary: 0xb782e5-0xb782f1 - return nil
	return nil
}
