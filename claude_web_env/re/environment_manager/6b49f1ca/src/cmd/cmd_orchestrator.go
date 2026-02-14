// Reconstructed from binary: environment-manager (Build ID 6b49f1ca)
// Source: cmd/cmd_orchestrator.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/cmd

package cmd

import (
	"time"

	"github.com/spf13/cobra"
)

// AddOrchestratorCommand adds the "orchestrator" subcommand to the root cobra
// command. The orchestrator command runs a long-lived polling loop that
// receives sessions from the API and dispatches them to hook commands.
//
// Binary: 0xb730c0 - cmd.AddOrchestratorCommand
// Source: cmd/cmd_orchestrator.go
//
// Parameters:
//   AX = *cobra.Command (parent/root command)
//
// Flags registered (from pflag calls in disassembly):
//   --api-url           (0x07=7 chars) StringVar, default "https://api.anthropic.com" (0x19=25), desc (0x0c=12)
//   --secret-path       (0x0f=15 chars) StringVar, default "", desc (0x36=54 chars)
//   --session-id        (0x0e=14 chars) StringVar, default "", desc (0x3f=63 chars)
//   --work-id           (0x09=9 chars) StringVar, default "", desc (0x30=48 chars)
//   --poll-interval     (0x0c=12 chars) DurationVar, default 5min, desc (0x1d=29 chars)
//   --hook-timeout      (0x0c=12 chars) DurationVar, default 5min, desc (0x2b=43 chars)
//   --max-poll-retries  (0x11=17 chars) IntVar, default 0, desc (0x3f=63 chars)
//   --max-hook-retries  (0x15=21 chars) IntVar, default 0, desc (0x63=99 chars)
//   --hook-command      (0x0c=12 chars) StringVar, default "", desc (0x37=55 chars)
//   --task-command      (0x0c=12 chars) StringVar, default "", desc (0xc3=195 chars - long)
//   --session-timeout   (0x14=20 chars) DurationVar, default 5min, desc (0x3b=59 chars)
//   --session-backoff   (0x14=20 chars) StringVar, default "", desc longer
//   --sandbox-backend   (0x0f=15 chars) StringVar
//   --log-file          StringVar
//   --mode              StringVar
func AddOrchestratorCommand(rootCmd *cobra.Command) {
	// Allocate flag storage variables.
	// Binary: 0xb730e5-0xb73240 - many runtime.newobject + mallocgc calls
	// Creates ~15 flag variable pointers for string, int, duration, bool types
	var apiURL string          // offset 0xf8
	var secretPath string      // offset 0xc0
	var sessionID string       // offset 0xe8
	var workID string          // offset 0xf0
	var pollInterval time.Duration // offset 0x70 (mallocgc 16 bytes)
	var hookTimeout time.Duration  // offset 0xc8 (via DurationVar)
	var maxPollRetries int     // offset 0x68 (mallocgc 16 bytes)
	var maxHookRetries int     // offset 0xb0 (via IntVar)
	var hookCommand string     // offset 0x80
	var taskCommand string     // offset 0xe0
	var sessionTimeout time.Duration // offset 0x60 (mallocgc 16 bytes)
	var sessionBackoff time.Duration // offset 0x58 (mallocgc 8 bytes)
	var sandboxBackend string  // offset 0xd0
	var logFile string         // offset 0xa8
	var mode string            // offset 0xa0
	var sandboxEnabled bool
	var logLevel string        // offset 0x98

	// Create the cobra.Command.
	// Binary: 0xb73240-0xb73298
	// Use: "orchestrator" (0x0c=12 chars)
	// Short: 0x31=49 chars
	// Long: 0x3fc=1020 chars
	// Example: 0x34c=844 chars
	orchCmd := &cobra.Command{
		Use:   "orchestrator",
		Short: "Run the session orchestrator polling loop",
		Long: `Run the session orchestrator that polls for available sessions and dispatches them to hook commands. The orchestrator runs as a long-lived process, continuously polling the API for work and executing the configured hook command when sessions are received. It supports configurable poll intervals, timeouts, retry policies, and sandbox wrapping.`,
		Example: `  # Basic usage (identity discovered via whoami, worker-id defaults to hostname)
  environment-runner orchestrator --api-url=https://api.example.com --secret-path=/path/to/key --session-id=abc

  # With custom hook command
  environment-runner orchestrator --api-url=https://api.example.com --secret-path=/path/to/key --session-id=abc --hook-command="/usr/bin/my-hook"

  # Basic usage with sandbox (recommended - identity discovered via whoami)
  environment-runner orchestrator --api-url=https://api.example.com --secret-path=/path/to/key --session-id=abc --sandbox-backend=bubblewrap`,
		RunE: func(cmd *cobra.Command, args []string) error {
			// Binary: 0xb73960 - AddOrchestratorCommand.func1
			// func1.1 at 0xb75e00 is an inner closure
			_ = apiURL
			_ = secretPath
			_ = sessionID
			_ = workID
			_ = pollInterval
			_ = hookTimeout
			_ = maxPollRetries
			_ = maxHookRetries
			_ = hookCommand
			_ = taskCommand
			_ = sessionTimeout
			_ = sessionBackoff
			_ = sandboxBackend
			_ = logFile
			_ = mode
			_ = sandboxEnabled
			_ = logLevel
			return nil
		},
	}

	// Register all flags.
	// Binary: 0xb734ca-0xb73920+
	orchCmd.Flags().StringVar(&apiURL, "api-url", "https://api.anthropic.com", "API base URL")
	orchCmd.Flags().StringVar(&secretPath, "secret-path", "", "Path to environment service key file for API authentication. Falls back to ENVIRONMENT_SERVICE_KEY env var if not set.")
	orchCmd.Flags().StringVar(&sessionID, "session-id", "", "Session identifier for polling. Required for session-based orchestration.")
	orchCmd.Flags().StringVar(&workID, "work-id", "", "Work identifier for acknowledging work items from the API.")
	orchCmd.Flags().DurationVar(&pollInterval, "poll-interval", 5*time.Minute, "Interval between poll requests")
	orchCmd.Flags().DurationVar(&hookTimeout, "hook-timeout", 5*time.Minute, "Maximum time to wait for hook command execution")
	orchCmd.Flags().IntVar(&maxPollRetries, "max-poll-retries", 0, "Maximum number of consecutive poll failures before giving up (0 = unlimited)")
	orchCmd.Flags().IntVar(&maxHookRetries, "max-hook-retries", 0, "Maximum number of hook execution retries before reporting failure. Each retry uses exponential backoff.")
	orchCmd.Flags().StringVar(&hookCommand, "hook-command", "", "Command to run when a session is received from polling")
	orchCmd.Flags().StringVar(&taskCommand, "task-command", "", "Command to run with session JSON via stdin when task received. If not provided, defaults to self-invoking 'task-run --stdin --input-format=v1' (with or without sandbox based on --sandbox-backend)")
	orchCmd.Flags().DurationVar(&sessionTimeout, "session-timeout", 5*time.Minute, "Maximum time to wait for a session before timing out")
	orchCmd.Flags().StringVar(&sessionBackoff, "session-backoff", "", "Backoff strategy for session polling")
	orchCmd.Flags().StringVar(&sandboxBackend, "sandbox-backend", "", "Sandbox backend to use for task execution (e.g., bubblewrap)")
	orchCmd.Flags().StringVar(&logFile, "log-file", "", "Path to log file for diagnostic output")
	orchCmd.Flags().StringVar(&mode, "mode", "", "Operating mode")
	orchCmd.Flags().StringVar(&logLevel, "log-level", "", "Log level (debug, info, warn, error)")

	// Add to root command.
	rootCmd.AddCommand(orchCmd)
}
