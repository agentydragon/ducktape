// Reconstructed from binary: environment-manager (Build ID 64bc4dc1)
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
	var clientID string
	var environmentID string
	var executeHook string
	var executeHookTimeout time.Duration
	var logLevel string
	var loopTimeout time.Duration
	var maxPollFailures int
	var organizationID string
	var pollHook string
	var pollHookTimeout time.Duration
	var pollTimeout time.Duration
	var reclaimOlderThanMs int
	var sandboxBackend string
	var sandboxSettings string
	var serviceKeyFile string
	var skipContainerLock bool
	var skipGitConfig bool
	var timeoutHook string
	var timeoutHookTimeout time.Duration

	orchCmd := &cobra.Command{
		Use:   "orchestrator",
		Short: "Poll for session tasks and hand off for execution",
		Long: `The orchestrator command polls the API for session tasks and handing off work to be executed.

It handles:
- Discovering environment identity via the /v1/environments/whoami endpoint
- Polling the environments API work/poll endpoint
- Executing work:
  - With --execute-hook: pipes JSON to hook via stdin and exits with hook's exit code
  - Without --execute-hook: auto-invokes 'task-run --input-format=v1'
    - With --sandbox-backend=sandbox-runtime (default): wraps execution in sandbox
    - With --sandbox-backend=none: runs without sandbox (logs warning)
- Running timeout hooks for periodic maintenance (e.g., monorepo updates)
- Sleeping with jitter when queue is empty
- Graceful shutdown on SIGTERM/SIGINT

Required environment variable:
  ENVIRONMENT_SERVICE_KEY: Service key for the environment

The environment ID and organization ID are discovered automatically via the whoami
endpoint. You can optionally provide --environment-id and --organization-id flags
to validate them against the token's identity.`,
		Example: `  # Basic usage with sandbox (recommended - identity discovered via whoami)
  export ENVIRONMENT_SERVICE_KEY="your-environment-service-key"
  environment-runner orchestrator

  # With explicit IDs for validation
  export ENVIRONMENT_SERVICE_KEY="your-environment-service-key"
  environment-runner orchestrator \
    --environment-id "env_01ABC123" \
    --organization-id "org_01XYZ789"

  # With custom execute hook to handle sessions
  export ENVIRONMENT_SERVICE_KEY="your-environment-service-key"
  environment-runner orchestrator \
    --environment-id "env_01ABC123" \
    --organization-id "org_01XYZ789" \
    --execute-hook "./handle-session.sh"

  # Without sandbox (auto-invokes task-run without sandboxing)
  export ENVIRONMENT_SERVICE_KEY="your-environment-service-key"
  environment-runner orchestrator \
    --sandbox-backend none`,
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
				"service-key-file", serviceKeyFile,
				"environment-id", environmentID,
				"client-id", clientID,
				"sandbox-backend", sandboxBackend,
				"poll-timeout", pollTimeout,
			)

			// Step 3: Acquire container-level lock (unless --skip-container-lock).
			// Binary: 0xb73e86 AcquireContainerLock
			// Binary: 0xbaf8df cmpb $0x0,(%rcx) — checks skipContainerLock bool
			// Binary: 0xbaf8e2 je baf91f — if false, jumps to lock acquisition
			// Binary: 0xbaf8e4-0xbaf91a — if true, logs warning then jmp past lock
			if skipContainerLock {
				log.Warn("Skipping container-level lock (--skip-container-lock)")
			} else {
				cleanup, err := util.AcquireContainerLock(context.Background(), "orchestrator", environmentID)
				if err != nil {
					return fmt.Errorf("failed to acquire container lock: %w", err)
				}
				defer cleanup()
			}

			// Step 4: Read secret from file or env var.
			// Binary: 0xb73f60 os.ReadFile, 0xb74065 TrimSpace, 0xb74080 os.Getenv
			var secret string
			if serviceKeyFile != "" {
				data, err := os.ReadFile(serviceKeyFile)
				if err != nil {
					return fmt.Errorf("failed to read secret file: %w", err)
				}
				secret = strings.TrimSpace(string(data))
			} else {
				secret = os.Getenv("ENVIRONMENT_SERVICE_KEY")
			}

			// Step 5: Discover identity via whoami.
			// Binary: 0xb740c0 NewWhoamiClient, 0xb740e4 GetIdentity
			whoamiClient := orchestrator.NewWhoamiClient(apiURL, secret, environmentID, log)
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
			// If pollHook is set, use PollHook; otherwise use regular Poller
			var poller orchestrator.PollerInterface
			if pollHook != "" {
				// Create PollHook when poll hook command is specified
				poller = orchestrator.NewPollHook(nil, pollHook, pollHookTimeout, nil, sandboxBackend != "" && sandboxBackend != "none", sandboxBackend, nil, log)
			} else {
				// Create regular Poller when no hook command
				poller = orchestrator.NewPollerWithWorkerID(apiURL, environmentID, secret, serviceKeyFile, clientID, log)
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
			// Binary: 0xbb0ee1 NewOrchestrator
			// Args (register ABI): AX,BX=poller (PollerInterface),
			//   CX,DI=environmentID, SI=pollTimeout, R8=loopTimeout,
			//   R9=maxPollFailures, R10=log
			// NOTE: executeHook is NOT passed here — it's already wrapped
			// inside the PollHook (created in Step 7 when pollHook != "").
			orch, err := orchestrator.NewOrchestrator(poller, environmentID, pollTimeout, loopTimeout, maxPollFailures, log)
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
	orchCmd.Flags().StringVar(&clientID, "client-id", "", "Client ID (worker identifier, default: hostname)")
	orchCmd.Flags().StringVar(&environmentID, "environment-id", "", "Environment ID (find at claude.ai/settings, e.g., env_01ABC123)")
	orchCmd.Flags().StringVar(&executeHook, "execute-hook", "", "Command to run with session JSON via stdin when task received. If not provided, defaults to self-invoking 'task-run --stdin --input-format=v1' (with or without sandbox based on --sandbox-backend)")
	orchCmd.Flags().DurationVar(&executeHookTimeout, "execute-hook-timeout", 0, "Timeout for execute hook (0 = no timeout, session lifetime controlled by API)")
	orchCmd.Flags().StringVar(&logLevel, "log-level", "info", "Log level (debug, info, warn, error)")
	orchCmd.Flags().DurationVar(&loopTimeout, "loop-timeout", 5*time.Minute, "Loop timeout before triggering timeout hook")
	orchCmd.Flags().IntVar(&maxPollFailures, "max-poll-failures", 0, "Maximum consecutive poll failures before exiting (0 = infinite)")
	orchCmd.Flags().StringVar(&organizationID, "organization-id", "", "Organization ID (find at claude.ai/settings > Account)")
	orchCmd.Flags().StringVar(&pollHook, "poll-hook", "", "Command to execute for polling instead of built-in Poller. Receives environment context via stdin, returns work JSON via stdout.")
	orchCmd.Flags().DurationVar(&pollHookTimeout, "poll-hook-timeout", 30*time.Second, "Timeout for poll hook execution")
	orchCmd.Flags().DurationVar(&pollTimeout, "poll-timeout", 5*time.Minute, "Poll request timeout duration")
	orchCmd.Flags().IntVar(&reclaimOlderThanMs, "reclaim-older-than-ms", 0, "Reclaim unacknowledged work items older than this many milliseconds (0 = use API default of 5000ms)")
	orchCmd.Flags().StringVar(&sandboxBackend, "sandbox-backend", "sandbox-runtime", `Sandbox backend for execute hook: none, sandbox-runtime.
Use 'none' to disable sandboxing (allows running as non-root user).
Use 'sandbox-runtime' (default) for sandboxed execution (requires root or unprivileged user namespaces).`)
	orchCmd.Flags().StringVar(&sandboxSettings, "sandbox-settings", "", "Path to custom sandbox-runtime settings JSON file (must include Anthropic domains)")
	orchCmd.Flags().StringVar(&serviceKeyFile, "service-key-file", "", "Path to file containing the environment service key. If not set, falls back to ENVIRONMENT_SERVICE_KEY environment variable")
	orchCmd.Flags().BoolVar(&skipContainerLock, "skip-container-lock", false, "Skip container-level lock (WARNING: allows multiple sessions per container, use only for development/testing)")
	orchCmd.Flags().BoolVar(&skipGitConfig, "skip-git-config", false, "Skip git configuration setup (use container's existing .gitconfig)")
	orchCmd.Flags().StringVar(&timeoutHook, "timeout-hook", "", "Command to run on loop timeout (e.g., monorepo updates)")
	orchCmd.Flags().DurationVar(&timeoutHookTimeout, "timeout-hook-timeout", 5*time.Minute, "Timeout for timeout hook execution (e.g., monorepo updates)")

	rootCmd.AddCommand(orchCmd)
}
