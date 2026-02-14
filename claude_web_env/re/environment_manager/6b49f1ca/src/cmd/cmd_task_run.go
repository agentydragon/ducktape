// Reconstructed from binary: environment-manager (Build ID 6b49f1ca)
// Source: cmd/cmd_task_run.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/cmd

package cmd

import (
	"github.com/spf13/cobra"
)

// AddTaskRunCommand adds the "task-run" subcommand to the root cobra command.
// The task-run command executes a script or command as part of a session,
// with support for stdin-based session context, sandbox wrapping, and
// work acknowledgment.
//
// Binary: 0xb78340 - cmd.AddTaskRunCommand
// Source: cmd/cmd_task_run.go
//
// Parameters:
//   AX = *cobra.Command (parent/root command)
//
// Flags registered (from pflag calls):
//   --api-url           (0x07=7 chars) StringVar, desc (0x26=38 chars)
//   --work-id           (0x09=9 chars) StringVar, default "mode" (0x04), desc (0x24=36 chars)
//   --stdin             (0x05=5 chars) BoolVarP, default false, desc (0x3b=59 chars)
//   --output-file       (0x0c=12 chars) StringVar, short "o" (0x02), desc (0x6f=111 chars)
//   --working-dir       (0x0c=12 chars) StringVar, short "dir" (0x03), desc (0xa0=160 chars)
//   --script-path       (0x0d=13 chars) StringVar, desc (0xa0=160 chars)
//   --input-format      (0x0d=13 chars) BoolVarP, desc (0x65=101 chars)
//   --sandbox-enabled   (0x13=19 chars) BoolVarP, default 1, desc (0x6d=109 chars)
//   --sandbox-command   (0x14=20 chars) StringVar, desc (0xcd=205 chars)
//   --debug             (0x05=5 chars) BoolVarP, default false, desc (0x32=50 chars)
//   --sandbox-disabled  (0x13=19 chars) BoolVarP, desc (0x65=101 chars)
//   --sandbox-backend   (0x0f=15 chars) BoolVarP, desc (0x43=67 chars)
//   --log-file          StringVar
//   --secret-path       StringVar
func AddTaskRunCommand(rootCmd *cobra.Command) {
	// Allocate flag storage variables.
	// Binary: 0xb78365-0xb78492 - many runtime.newobject + mallocgc calls
	var apiURL string          // offset 0xb0
	var workID string          // offset 0xc8
	var stdin bool             // offset 0x78[0] - byte 0 of 7-byte bool block
	var outputFile string      // offset 0xe0
	var workingDir string      // offset 0xa8
	var scriptPath string      // offset 0x108
	var inputFormat bool       // offset 0x78[1-2] - bytes 1-2
	var sandboxEnabled bool    // offset 0x78[3-5] - bytes 3-5
	var sandboxCommand string  // offset 0x100
	var debug bool             // offset 0x78[6] - byte 6
	var sandboxDisabled bool
	var sandboxBackend string  // offset 0x80
	var logFile string         // offset 0xe8
	var secretPath string      // offset 0xc0
	var logLevel string        // offset 0xd8
	var enableTelemetry bool
	var metricsEnabled bool
	var sessionID string
	var mode string
	var secretKeyVar string    // offset 0xf8

	// Create the cobra.Command.
	// Binary: 0xb78492-0xb784db
	// Use: "task-run" (0x08=8 chars)
	// Short: 0x27=39 chars
	// Long: 0x188=392 chars
	taskRunCmd := &cobra.Command{
		Use:   "task-run",
		Short: "Execute a task script within a session",
		Long:  `Execute a task script within a session context. This command runs a specified script or reads session context from stdin, optionally wrapping execution in a sandbox for security. It supports work acknowledgment, output file capture, and configurable sandbox backends.`,
		RunE: func(cmd *cobra.Command, args []string) error {
			// Binary: 0xb78b80 - AddTaskRunCommand.func1
			// Contains func1.1 at 0xb7b0a0 and func1.3 at 0xb7af60
			_ = apiURL
			_ = workID
			_ = stdin
			_ = outputFile
			_ = workingDir
			_ = scriptPath
			_ = inputFormat
			_ = sandboxEnabled
			_ = sandboxCommand
			_ = debug
			_ = sandboxDisabled
			_ = sandboxBackend
			_ = logFile
			_ = secretPath
			_ = logLevel
			_ = enableTelemetry
			_ = metricsEnabled
			_ = sessionID
			_ = mode
			_ = secretKeyVar
			return nil
		},
	}

	// Register all flags.
	// Binary: 0xb786e9-0xb78999+
	taskRunCmd.Flags().StringVar(&apiURL, "api-url", "", "Base URL for the API for work acknowledgment")
	taskRunCmd.Flags().StringVar(&workID, "work-id", "mode", "The work ID for acknowledging work items from the API")
	taskRunCmd.Flags().BoolVar(&stdin, "stdin", false, "Read session context from stdin instead of using script-path. When enabled, expects JSON on stdin.")
	taskRunCmd.Flags().StringVar(&outputFile, "output-file", "v1", "Path to write task output. Short flag: -o. If not specified, output goes to stdout. When specified, captures script stdout/stderr to this file.")
	taskRunCmd.Flags().StringVar(&workingDir, "working-dir", "dir", "Working directory for script execution. Short flag: -d. Defaults to current directory. The script will be executed with this as its working directory.")
	taskRunCmd.Flags().StringVar(&scriptPath, "script-path", "", "Path to the script to execute. Required unless --stdin is used. The script must be executable.")
	taskRunCmd.Flags().BoolVar(&inputFormat, "input-format", false, "Input format version for stdin parsing. When set, uses the v1 JSON format for session context.")
	taskRunCmd.Flags().BoolVar(&sandboxEnabled, "sandbox-enabled", true, "Enable sandbox wrapping for script execution. When enabled, the script runs inside a security sandbox.")
	taskRunCmd.Flags().StringVar(&sandboxCommand, "sandbox-command", "", "Custom sandbox command to use for wrapping script execution. Overrides the default sandbox binary. Supports multiple sandbox backends with configurable security profiles.")
	taskRunCmd.Flags().BoolVar(&debug, "debug", false, "Enable debug mode with verbose logging for troubleshooting")
	taskRunCmd.Flags().BoolVar(&sandboxDisabled, "sandbox-disabled", false, "Explicitly disable sandbox wrapping for script execution. Overrides --sandbox-enabled.")
	taskRunCmd.Flags().BoolVar(&sandboxBackend, "sandbox-backend", false, "Sandbox backend to use (e.g., bubblewrap, firecracker)")
	taskRunCmd.Flags().StringVar(&logFile, "log-file", "", "Path to log file")
	taskRunCmd.Flags().StringVar(&secretPath, "secret-path", "", "Path to secret key file")

	// Add to root command.
	rootCmd.AddCommand(taskRunCmd)
}

// loadContextFromStdin reads and parses session context from stdin.
//
// Binary: 0xb7b1e0 - cmd.loadContextFromStdin
// Source: cmd/cmd_task_run.go
func loadContextFromStdin() (interface{}, error) {
	// Reads JSON from stdin and parses into session context struct
	return nil, nil
}

// acknowledgeWorkIfNeeded sends a work acknowledgment to the API if a work ID
// is configured.
//
// Binary: 0xb7bb20 - cmd.acknowledgeWorkIfNeeded
// Source: cmd/cmd_task_run.go
func acknowledgeWorkIfNeeded(apiURL, workID, secretPath string) error {
	// Calls the API to acknowledge work completion
	// Error message observed: "cannot ACK work %s: missing auth context"
	return nil
}

// stdinConfigClient implements the config client interface by reading
// session configuration from stdin input.
type stdinConfigClient struct {
	// Wraps stdin-parsed session data
}

// GetEnvironmentForSession returns the environment configuration from stdin data.
//
// Binary: 0xb7b8e0 - (*stdinConfigClient).GetEnvironmentForSession
// Source: cmd/cmd_task_run.go
func (s *stdinConfigClient) GetEnvironmentForSession() (interface{}, error) {
	return nil, nil
}

// GetAuthContext returns the auth context from stdin data.
//
// Binary: 0xb7bae0 - (*stdinConfigClient).GetAuthContext
// Source: cmd/cmd_task_run.go
func (s *stdinConfigClient) GetAuthContext() (interface{}, error) {
	return nil, nil
}

// GetOutcomes returns outcomes from stdin data.
//
// Binary: 0xb7bb00 - (*stdinConfigClient).GetOutcomes
// Source: cmd/cmd_task_run.go
func (s *stdinConfigClient) GetOutcomes() (interface{}, error) {
	return nil, nil
}
