// Reconstructed from binary: environment-manager (Build ID 6b49f1ca)
// Source: cmd/cmd_orchestrator.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/cmd

package cmd

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/signal"
	"strings"
	"syscall"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/logger"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/orchestrator"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/sandbox"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/util"
	"github.com/spf13/cobra"
)

// AddOrchestratorCommand adds the "orchestrator" subcommand to the root cobra
// command. The orchestrator command runs a long-lived polling loop that
// receives sessions from the API and dispatches them to hook commands.
//
// Binary: 0xb730c0 - cmd.AddOrchestratorCommand
// Source: cmd/cmd_orchestrator.go
func AddOrchestratorCommand(rootCmd *cobra.Command) {
	var apiURL string
	var secretPath string
	var sessionID string
	var workID string
	var pollInterval time.Duration
	var hookTimeout time.Duration
	var maxPollRetries int
	var maxHookRetries int
	var hookCommand string
	var taskCommand string
	var sessionTimeout time.Duration
	var sessionBackoff time.Duration
	var sandboxBackend string
	var logFile string
	var mode string
	var sandboxEnabled bool
	var logLevel string

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
			// Binary: 0xb73960 - AddOrchestratorCommand.func1 (1577 lines of asm)

			// Step 1: Parse log level and create logger.
			// Binary: 0xb73a95 parseLogLevel, 0xb73ab4 CreateLoggerWithFileOutput
			level, err := parseLogLevel(logLevel)
			if err != nil {
				return err
			}
			log := logger.CreateLoggerWithFileOutput(level)

			// Step 2: Log all configuration values.
			// Binary: 0xb73ad0-0xb73dd4 — builds 7 slog.Attr key-value pairs and calls log.Info
			log.Info("Starting orchestrator",
				"api-url", apiURL,
				"secret-path", secretPath,
				"session-id", sessionID,
				"work-id", workID,
				"sandbox-backend", sandboxBackend,
				"poll-interval", pollInterval,
				"sandbox-enabled", sandboxEnabled,
			)

			// Step 3: If sandbox enabled, log long sandbox info message.
			// Binary: 0xb73e1f CMPB sandbox_enabled, 0xb73e55 log call with 0xd6-length string
			if sandboxEnabled {
				log.Info("Sandbox mode enabled. The orchestrator will wrap task execution in a sandboxed environment. This provides isolation for running untrusted code. The sandbox backend determines the isolation technology used (e.g., bubblewrap for Linux namespaces).")
			}

			// Step 4: Acquire container-level lock.
			// Binary: 0xb73e86 AcquireContainerLock
			cleanup, err := util.AcquireContainerLock(context.Background(), "orchestrator", sessionID)
			if err != nil {
				return fmt.Errorf("failed to acquire container lock: %w", err)
			}
			defer cleanup()

			// Step 5: Read secret from file or env var.
			// Binary: 0xb73f60 os.ReadFile, 0xb74065 TrimSpace, 0xb74080 os.Getenv
			var secret string
			if secretPath != "" {
				data, err := os.ReadFile(secretPath)
				if err != nil {
					return fmt.Errorf("failed to read secret file: %w", err)
				}
				secret = strings.TrimSpace(string(data))
			} else {
				secret = os.Getenv("ENVIRONMENT_SERVICE_KEY")
			}

			// Step 6: Discover identity via whoami.
			// Binary: 0xb740c0 NewWhoamiClient, 0xb740e4 GetIdentity
			whoamiClient := orchestrator.NewWhoamiClient(apiURL, secret, sessionID, log)
			identity, err := whoamiClient.GetIdentity(context.Background())
			if err != nil {
				log.Warn("Failed to get identity via whoami",
					"error", err,
				)
				return fmt.Errorf("failed to get identity: %w", err)
			}

			log.Info("Discovered orchestrator identity",
				"identity", identity,
			)

			// Step 7: Create poller (either PollHook or regular Poller).
			// Binary: 0xb7470a NewPollHook, 0xb7476c NewPollerWithWorkerID
			// If hookCommand is set, use PollHook; otherwise use regular Poller
			var poller orchestrator.PollerInterface
			if hookCommand != "" {
				// Create PollHook when hook command is specified
				poller = orchestrator.NewPollHook(nil, hookCommand, hookTimeout, nil, sandboxEnabled, sandboxBackend, nil, log)
			} else {
				// Create regular Poller when no hook command
				poller = orchestrator.NewPollerWithWorkerID(apiURL, sessionID, secret, secretPath, workID, log)
			}

			// Step 8: Install sandbox runtime if configured.
			// Binary: 0xb74ac0 os.Getenv, 0xb74af0 InstallSandboxRuntime
			if sandboxBackend != "" {
				log.Info("Installing sandbox runtime",
					"backend", sandboxBackend,
				)
				if err := sandbox.InstallSandboxRuntime(log, context.Background(), sandboxBackend); err != nil {
					return fmt.Errorf("failed to install sandbox runtime: %w", err)
				}
			}

			// Step 9: Get working directory.
			// Binary: 0xb74e03 os.Getwd
			workDir, err := os.Getwd()
			if err != nil {
				return fmt.Errorf("failed to get working directory: %w", err)
			}

			log.Info("Orchestrator configured",
				"work_dir", workDir,
			)

			// Step 10: Create orchestrator.
			// Binary: 0xb75421 NewOrchestrator
			orch, err := orchestrator.NewOrchestrator(poller, sessionID, sessionTimeout, sessionBackoff, mode, log)
			if err != nil {
				return fmt.Errorf("failed to create orchestrator: %w", err)
			}

			// Step 11: Set up signal handling for graceful shutdown.
			// Binary: 0xb754d4 context.WithCancel, 0xb75564 signal.Notify
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()

			sigCh := make(chan os.Signal, 1)
			signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)

			go func() {
				sig := <-sigCh
				log.Info("Received shutdown signal",
					"signal", sig,
				)
				cancel()
			}()

			// Step 12: Run the orchestrator loop.
			// Binary: 0xb75917 time.NewTimer for session timeout
			err = orch.Run(ctx)
			if err != nil {
				if errors.Is(err, context.Canceled) {
					log.Info("Orchestrator stopped due to cancellation")
					return nil
				}
				log.Warn("Orchestrator exited with error",
					"error", err,
				)
				return fmt.Errorf("orchestrator failed: %w", err)
			}

			log.Info("Orchestrator exited cleanly")
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
	orchCmd.Flags().DurationVar(&sessionBackoff, "session-backoff", 0, "Backoff duration between session polling cycles")
	orchCmd.Flags().StringVar(&sandboxBackend, "sandbox-backend", "", "Sandbox backend to use for task execution (e.g., bubblewrap)")
	orchCmd.Flags().StringVar(&logFile, "log-file", "", "Path to log file for diagnostic output")
	orchCmd.Flags().StringVar(&mode, "mode", "", "Operating mode")
	orchCmd.Flags().BoolVar(&sandboxEnabled, "sandbox-enabled", false, "Enable sandbox mode for task execution")
	orchCmd.Flags().StringVar(&logLevel, "log-level", "", "Log level (debug, info, warn, error)")

	rootCmd.AddCommand(orchCmd)
}
