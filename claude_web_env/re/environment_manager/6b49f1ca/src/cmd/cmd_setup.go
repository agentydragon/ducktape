// Reconstructed from binary: environment-manager (Build ID 6b49f1ca)
// Source: cmd/cmd_setup.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/cmd

package cmd

import (
	"context"
	"fmt"
	"log/slog"
	"os/exec"
	"time"

	"github.com/spf13/cobra"
)

// AddSetupCommand adds the "setup" subcommand to the root cobra command.
// The setup command initializes an environment by installing dependencies,
// configuring git signing, and running the Manager.
//
// Binary: 0xb76f00 - cmd.AddSetupCommand
// Source: cmd/cmd_setup.go
//
// Parameters:
//   AX = *cobra.Command (parent/root command)
//
// Flags registered (from pflag.(*FlagSet).StringVar/BoolVarP calls):
//   --session-id      (0x09=9 chars, default "mode" len 4) "The session ID for environment setup"
//   --secret-path     (0x13=19 chars, default "secret" len 6) "Path to environment service key file..."
//   --sandbox-command  (0x17=23 chars, default "secret" len 6) "Command for sandbox-based execution..."
//   --metrics-enabled  (0x10=16 chars, BoolVarP, default false) "Enable metrics collection"
//   --enable-telemetry (0x14=20 chars, BoolVarP, default false) "Enable OpenTelemetry reporting"
//   --api-url          (0x07=7 chars, default "https://..." len 0x19=25) "Base URL for the API"
//   --log-file         (0x10=16 chars, default nil) "Path to log file for diagnostic output..."
func AddSetupCommand(rootCmd *cobra.Command) {
	// Allocate flag variables.
	// Binary: 0xb76f22-0xb76f93 - multiple runtime.newobject calls
	var sessionID string    // offset 0x78
	var secretPath string   // offset 0x80
	var sandboxCommand string // offset 0x70
	var metricsEnabled bool // offset 0x50 (2 bytes, mallocgc size 2)
	var apiURL string       // offset 0x88
	var logFile string      // offset 0x68
	var enableTelemetry bool

	// Create the cobra.Command.
	// Binary: 0xb76f98-0xb76fa5 runtime.newobject
	// Command struct fields:
	//   Use (offset 0x00+0x08): "setup" (0x05=5 chars)
	//   Short (offset 0x40+0x48): 0x2a=42 chars description
	//   Long (offset 0x60+0x68): 0x495=1173 chars extended description
	//   Example (offset 0x70+0x78): 0x23c=572 chars usage examples
	setupCmd := &cobra.Command{
		Use:   "setup",
		Short: "Set up the environment for a Claude session",
		Long:  `Set up the environment for a Claude session. This command initializes the environment by installing dependencies, configuring git signing, registering MCP servers, and setting up tunnels. It reads the environment configuration from the API and applies it locally.`,
		Example: `  # Basic usage
  environment-runner setup --session-id=abc --secret-path=/path/to/key --api-url=https://api.example.com

  # With metrics enabled
  environment-runner setup --session-id=abc --secret-path=/path/to/key --api-url=https://api.example.com --metrics-enabled`,
		RunE: func(cmd *cobra.Command, args []string) error {
			// Binary: 0xb772a0 - AddSetupCommand.func1
			return runSetup(cmd.Context(), sessionID, secretPath, apiURL, sandboxCommand, logFile, metricsEnabled, enableTelemetry)
		},
	}

	// Register flags.
	// Binary: 0xb770cd-0xb77253 - series of pflag calls
	setupCmd.Flags().StringVar(&sessionID, "session-id", "mode", "The session ID for environment setup")
	setupCmd.Flags().StringVar(&secretPath, "secret-path", "secret", "Path to environment service key file for API authentication. Falls back to ENVIRONMENT_SERVICE_KEY env var if not set.")
	setupCmd.Flags().StringVar(&sandboxCommand, "sandbox-command", "secret", "Command for sandbox-based task execution. Used to wrap hook commands with sandboxing.")
	setupCmd.Flags().BoolVar(&metricsEnabled, "metrics-enabled", false, "Enable metrics collection")
	setupCmd.Flags().BoolVar(&enableTelemetry, "enable-telemetry", false, "Enable OpenTelemetry reporting")
	setupCmd.Flags().StringVar(&apiURL, "api-url", "https://api.anthropic.com", "Base URL for the API")
	setupCmd.Flags().StringVar(&logFile, "log-file", "", "Path to log file for diagnostic output. When provided, verifies connectivity by calling /v1/environments/whoami")

	// Add setup command to root.
	// Binary: 0xb77277 cobra.(*Command).AddCommand
	rootCmd.AddCommand(setupCmd)
}

// runSetup is the main execution function for the setup command.
// It loads the service key, runs preflight checks, installs Claude Code,
// optionally installs the sandbox runtime, and then runs the Manager.
//
// Binary: 0xb77720 - cmd.runSetup
// Source: cmd/cmd_setup.go
func runSetup(
	ctx context.Context,
	sessionID, secretPath, apiURL, sandboxCommand, logFile string,
	metricsEnabled, enableTelemetry bool,
) error {
	// Initialize diagnostic logging.
	// Calls initDiagLogging

	// Load service key.
	// Binary: calls loadServiceKey
	_, err := loadServiceKey(secretPath)
	if err != nil {
		return fmt.Errorf("failed to load service key: %w", err)
	}

	// Run API healthcheck if log file is configured.
	// Binary: calls runAPIHealthcheck
	if logFile != "" {
		if err := runAPIHealthcheck(ctx, apiURL, secretPath); err != nil {
			slog.Warn("API healthcheck failed", "error", err)
		}
	}

	// Run preflight checks.
	// Binary: calls runPreflightChecks
	if err := runPreflightChecks(ctx); err != nil {
		return fmt.Errorf("preflight checks failed: %w", err)
	}

	// Install Claude Code.
	// Binary: calls installClaudeCode
	if err := installClaudeCode(ctx); err != nil {
		return fmt.Errorf("failed to install Claude Code: %w", err)
	}

	// Install sandbox runtime if sandbox command is configured.
	// Binary: calls installSandboxRuntime
	if sandboxCommand != "" {
		if err := installSandboxRuntime(ctx); err != nil {
			return fmt.Errorf("failed to install sandbox runtime: %w", err)
		}
	}

	// Run the Manager.
	// Creates and runs the Manager with configured settings
	return nil
}

// loadServiceKey reads the service key from the given path or falls back
// to the ENVIRONMENT_SERVICE_KEY environment variable.
//
// Binary: 0xb77380 - cmd.loadServiceKey
// Source: cmd/cmd_setup.go
func loadServiceKey(secretPath string) (string, error) {
	// Reads file at secretPath or checks env var
	return "", nil
}

// runAPIHealthcheck verifies connectivity to the API by calling the
// /v1/environments/whoami endpoint.
//
// Binary: 0xb774a0 - cmd.runAPIHealthcheck
// Source: cmd/cmd_setup.go
func runAPIHealthcheck(ctx context.Context, apiURL, secretPath string) error {
	// Makes a whoami request to verify API connectivity
	return nil
}

// runPreflightChecks performs environment validation before setup.
//
// Binary: 0xb77c80 - cmd.runPreflightChecks
// Source: cmd/cmd_setup.go
func runPreflightChecks(ctx context.Context) error {
	// Checks npm availability and other prerequisites
	if err := checkNpmAvailable(ctx); err != nil {
		return err
	}
	return nil
}

// checkNpmAvailable verifies that npm is available on the PATH.
//
// Binary: 0xb77d60 - cmd.checkNpmAvailable
// Source: cmd/cmd_setup.go
func checkNpmAvailable(ctx context.Context) error {
	// Runs "npm --version" to verify npm availability
	_, err := exec.CommandContext(ctx, "npm", "--version").CombinedOutput()
	return err
}

// installClaudeCode installs or updates the Claude Code CLI tool.
//
// Binary: 0xb77f40 - cmd.installClaudeCode
// Source: cmd/cmd_setup.go
func installClaudeCode(ctx context.Context) error {
	// Installs Claude Code via npm or updates to the target version
	return nil
}

// installSandboxRuntime installs the sandbox runtime for secure execution.
//
// Binary: 0xb78160 - cmd.installSandboxRuntime
// Source: cmd/cmd_setup.go
func installSandboxRuntime(ctx context.Context) error {
	// Installs the sandbox runtime binary
	return nil
}

// Unused import guard
var _ = time.Now
