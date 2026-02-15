// Reconstructed from binary: environment-manager (Build ID 6b49f1ca)
// Source: cmd/cmd_task_run.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/cmd

package cmd

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"os"
	"time"

	"github.com/spf13/cobra"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/api"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/auth"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/claude"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/config"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/input"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/logger"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/manager"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/o11y"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/o11y/diag"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/session"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/util"
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
	var inputFormat string     // offset 0x78[1-2] - bytes 1-2
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

			// 0xb78c8f: Record start time for total duration tracking
			startTime := time.Now()

			// 0xb78cd1-0xb78d13: Set environment variables
			// Sets CLAUDE_CODE_SESSION_ID and CLAUDE_CODE_BASE_REF
			os.Setenv("CLAUDE_CODE_SESSION_ID", sessionID)
			os.Setenv("CLAUDE_CODE_BASE_REF", "")

			// 0xb78d20-0xb78d88: Handle --input-format flag changes
			// If the flag was explicitly set AND environment config is empty AND stdin=false,
			// set a default environment config of []byte("skip")
			flags := cmd.Flags()
			inputFormatChanged := flags.Changed("input-format")

			// 0xb78d8a-0xb78da0: Validate mutually exclusive flags
			// "--upgrade-claude-code and --claude-agent-version are mutually exclusive"
			// (128 chars error message)

			// 0xb78dea: Parse log level
			logLvl, err := parseLogLevel(logLevel)
			if err != nil {
				logLvl = slog.LevelInfo
			}

			// 0xb78df9: Create logger with file output
			slogger := logger.CreateLoggerWithFileOutput(logLvl)

			// 0xb78e0e-0xb78e20: Check if --stdin flag was changed (deprecated)
			stdinFlags := cmd.Flags()
			stdinChanged := stdinFlags.Changed("stdin")
			if stdinChanged {
				// 0xb78e45: Log deprecation warning
				slogger.Warn("The --stdin flag is deprecated and will be removed in a future release. Stdin is now always used.")
			}

			// 0xb78e8a: Acquire container-level lock
			lockRelease, err := util.AcquireLock(cmd.Context(), "task-run", sessionID)
			if err != nil {
				return fmt.Errorf("failed to acquire lock: %w", err)
			}
			defer lockRelease()

			// 0xb78f26: Parse session mode
			sessionMode, err := config.ParseSessionMode(mode)
			if err != nil {
				return fmt.Errorf("failed to parse session mode: %w", err)
			}

			// 0xb78fd0: Record stdin read start time
			stdinStartTime := time.Now()

			// 0xb79030: Load context from stdin
			parsedCtx, err := loadContextFromStdin(
				slogger,
				inputFormat,
				secretPath,
				secretKeyVar,
				sessionID,
			)
			if err != nil {
				return fmt.Errorf("failed to load context from stdin: %w", err)
			}

			// 0xb794c0: Log "Loaded context from stdin"
			slogger.Info("Loaded context from stdin")

			// 0xb78d20-0xb78d88: Apply input-format default behavior
			// If --input-format was explicitly set and environment config is empty
			// and stdin mode is disabled, set default environment config to "skip"
			if inputFormatChanged && parsedCtx != nil {
				if len(parsedCtx.EnvironmentConfig) == 0 && !stdin {
					parsedCtx.EnvironmentConfig = json.RawMessage([]byte("skip"))
				}
			}

			// 0xb794cc: Create session from parsed context
			sess := &config.Session{}
			if parsedCtx != nil {
				if parsedCtx.StartupContext != nil {
					sess.StartupContext = parsedCtx.StartupContext
				}
				if parsedCtx.SessionID != "" {
					sess.SessionID = parsedCtx.SessionID
				}
				if parsedCtx.WorkID != "" {
					sess.WorkID = parsedCtx.WorkID
				}
			}

			// 0xb7912e: Get ENVIRONMENT_SERVICE_KEY from env
			envServiceKey := os.Getenv("ENVIRONMENT_SERVICE_KEY")

			// 0xb79188: Acknowledge work if needed
			err = acknowledgeWorkIfNeeded(
				slogger,
				apiURL,
				secretPath,
				sess,
				workID,
				envServiceKey,
				parsedCtx != nil,
			)
			_ = err // ACK errors are logged but don't stop execution

			// 0xb791b4: Get ENVRUNNER_SKIP_CLAUDE_CODE env var
			skipClaudeCode := os.Getenv("ENVRUNNER_SKIP_CLAUDE_CODE")

			// 0xb79260: Measure stdin parse duration
			stdinParseDuration := time.Since(stdinStartTime)
			o11y.RecordDuration("env_manager.stdin_parse.duration_ms", nil, nil, float64(stdinParseDuration.Milliseconds()))

			// 0xb79583: Create HTTP client for session ingress and activity recorder
			var activityRecorder session.ActivityRecorder
			if parsedCtx != nil && parsedCtx.AuthContext != nil {
				sessionIngressToken := parsedCtx.AuthContext.GetSessionIngressToken()
				if sessionIngressToken != "" {
					httpClient := api.NewHttpClient(apiURL)
					ingressClient := &api.HttpSessionIngressClient{
						Client: httpClient,
						ApiKey: sessionIngressToken,
						Logger: slogger,
					}
					activityRecorder = session.NewActivityRecorder(ingressClient, slogger, sessionID)
				}
			}
			if activityRecorder == nil {
				// Create noop activity recorder if no session ingress token
				activityRecorder = &session.NoopActivityRecorder{}
			}

			// Note: SKIP_GIT_CONFIG env var is checked directly in setupGitConfig() and configureGitSigning()
			// No need to read it here.

			// 0xb79654-0xb79672: Log custom executable path if set
			if scriptPath != "" {
				slogger.Info("Using custom executable path from CLI flag",
					"script_path", scriptPath,
				)
			}

			// 0xb796c0-0xb796f2: Check if Claude Code installation should be skipped
			if skipClaudeCode == "true" {
				slogger.Info("Skipping Claude Code installation (ENVRUNNER_SKIP_CLAUDE_CODE=true)")
			} else {
				// 0xb79720-0xb79799: Install or update Claude Code with timeout
				installStart := time.Now()
				installCtx, installCancel := context.WithTimeout(context.Background(), 10*time.Minute)
				_, err = claude.InstallOrUpdateClaudeCode(slogger, installCtx, "", "", nil, nil)
				installCancel()
				installDuration := time.Since(installStart)
				o11y.RecordDuration("env_manager.claude_code_install.duration_ms", nil, nil, float64(installDuration.Milliseconds()))

				if err != nil {
					return fmt.Errorf("failed to ensure Claude Code is available: %w", err)
				}
			}

			// 0xb798ae: Initialize diagnostic logging
			diagService, diagCleanup, err := initDiagLogging(
				cmd.Context(),
				apiURL,
				envServiceKey,
				sessionID,
				slogger,
			)
			if err != nil {
				return fmt.Errorf("failed to initialize diagnostic logging: %w", err)
			}
			if diagCleanup != nil {
				defer diagCleanup()
			}

			// 0xb79969: Log env manager start event (no PII)
			diag.LogEnvManagerNoPII(diagService, "start_task_run", nil)

			// 0xb79996: Get OTEL endpoint from env (for telemetry)
			otelEndpoint := os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT")

			// 0xb79a5a: Initialize observability service with OTEL endpoint
			var o11yConfig *o11y.O11yConfig
			if otelEndpoint != "" {
				o11yConfig = &o11y.O11yConfig{
					Endpoint: otelEndpoint,
				}
			}
			o11yService, err := o11y.NewO11yService(context.Background(), o11yConfig)
			if err != nil {
				return fmt.Errorf("failed to initialize observability service: %w", err)
			}
			_ = o11yService // Already wired via singleton - accessed via o11y.GetO11yService()

			// 0xb79bc0: Increment start counter
			o11y.Increment(context.Background(), nil, nil)

			// 0xb79d38: Record startup duration metric
			o11y.RecordDuration("env_manager.startup.total", nil, nil, float64(time.Since(startTime).Milliseconds()))

			// 0xb79d91: Validate session configuration
			if err := sess.Validate(); err != nil {
				return fmt.Errorf("invalid configuration: %w", err)
			}

			// 0xb79f4a: Log "Starting environment manager" with session details
			slogger.Info("Starting environment manager",
				"session_id", sess.SessionID,
				"work_id", sess.WorkID,
			)

			// 0xb7a1a0: Log configured allowed tools
			if sess.StartupContext != nil && len(sess.StartupContext.AllowedTools) > 0 {
				slogger.Info("Configured allowed tools for Claude",
					"allowed_tools", sess.StartupContext.AllowedTools,
				)
			}

			// Check if this is a healthcheck
			isHealthcheck := parsedCtx != nil && parsedCtx.StartupContext == nil && parsedCtx.AuthContext == nil
			if isHealthcheck {
				// 0xb7ad16-0xb7ae2b: Healthcheck path
				slogger.Info("Running healthcheck")
				slogger.Info("Executing healthcheck")
				healthcheckDuration := time.Since(startTime)
				o11y.RecordDuration("env_manager.healthcheck.duration_ms", nil, nil, float64(healthcheckDuration.Milliseconds()))
				slogger.Info("Healthcheck completed successfully",
					"total_startup_duration_ms", time.Since(startTime).Milliseconds(),
				)
				return nil
			}

			// 0xb7a25a: Log append system prompt if configured
			if sess.StartupContext != nil && sess.StartupContext.AppendSystemPrompt != "" {
				slogger.Info("Configured local append system prompt")
			}

			// 0xb7a2b1-0xb7a380: Log various configuration states
			slogger.Debug("WebSocket disabled, Claude Code will run in standalone mode")
			if debug {
				slogger.Info("Enabled verbose Claude Code logging to console")
			}
			slogger.Debug("Enabled printing Claude Code logs on exit")

			// 0xb7a4ae-0xb7a4cc: Log stdin environment configuration
			if parsedCtx != nil && parsedCtx.EnvironmentConfig != nil {
				slogger.Info("Using environment configuration from stdin")
			}

			// Build stdinConfigClient for the manager
			stdinClient := &stdinConfigClient{
				parsedCtx:        parsedCtx,
				activityRecorder: activityRecorder,
			}

			// 0xb7a761-0xb7a7a0: Compute durations
			totalParseTime := time.Since(stdinStartTime)
			o11y.RecordDuration("env_manager.total_parse.duration_ms", nil, nil, float64(totalParseTime.Milliseconds()))
			totalSetupTime := time.Since(startTime)
			o11y.RecordDuration("env_manager.total_setup.duration_ms", nil, nil, float64(totalSetupTime.Milliseconds()))

			// 0xb7a881-0xb7a8a0: Log "Setup completed, starting manager execution"
			slogger.Info("Setup completed, starting manager execution",
				"total_startup_duration_ms", time.Since(startTime).Milliseconds(),
			)

			// 0xb7a8c0: Run the manager
			mgr := &manager.Manager{
				Ctx:           context.Background(),
				Logger:        slogger,
				Config:        stdinClient,
				SessionID:     sessionID,
				APIBaseURL:    apiURL,
				SessionConfig: parsedCtx,
				SessionMode:   string(sessionMode),
			}
			managerErr := mgr.Run(context.Background(), slogger)

			managerDuration := time.Since(startTime)
			o11y.RecordDuration("env_manager.manager_run.duration_ms", nil, nil, float64(managerDuration.Milliseconds()))

			if managerErr != nil {
				// 0xb7a9d9-0xb7aaa7: Manager execution failed
				slogger.Error("Environment manager execution failed",
					"error", managerErr,
					"total_startup_duration_ms", time.Since(startTime).Milliseconds(),
				)
				o11y.IncrementEnvManagerEnd("failure", nil, "", "", "", nil, nil)
				diag.LogEnvManagerNoPII(diagService, "end_task_run_failure", nil)
				return fmt.Errorf("environment manager failed: %w", managerErr)
			}

			// 0xb7ab03-0xb7abea: Manager completed successfully
			o11y.IncrementEnvManagerEnd("success", nil, "", "", "", nil, nil)
			diag.LogEnvManagerNoPII(diagService, "end_task_run", nil)

			slogger.Info("Environment manager completed successfully",
				"total_startup_duration_ms", time.Since(startTime).Milliseconds(),
			)

			// 0xb7af60-0xb7b000: Create and execute Claude Code executor
			// Binary: func1.3 closure creates ClaudeCodeExecutor via NewClaudeCodeExecutor
			// and calls Execute() via interface dispatch at 0xb7b1a6
			slogger.Info("Creating Claude Code executor")

			// Create outcomes tracker for executor
			outcomes := claude.NewOutcomes()

			// Create Claude Code executor
			// Binary: NewClaudeCodeExecutor call at 0xb7b000
			// TODO(re): config parameter should be *config.ClaudeConfig, but that type
			// hasn't been fully reconstructed yet. Using parsedCtx.StartupContext for now.
			executor := claude.NewClaudeCodeExecutor(
				slogger,              // logger
				context.Background(), // ctx
				parsedCtx.StartupContext, // config (TODO: should be *config.ClaudeConfig)
				outcomes,             // outcomes
				diagService,          // diagReporter
			)

			// Execute Claude Code process
			// Binary: Execute() interface call at 0xb7b1a6
			slogger.Info("Executing Claude Code")
			if err := executor.Execute(context.Background()); err != nil {
				slogger.Error("Claude Code execution failed",
					"error", err,
				)
				return fmt.Errorf("Claude Code execution failed: %w", err)
			}

			slogger.Info("Claude Code execution completed successfully")

			return nil
		},
	}

	// Register all flags.
	// Binary: 0xb786e9-0xb78999+
	taskRunCmd.Flags().StringVar(&apiURL, "api-url", "", "Base URL for the API for work acknowledgment")
	taskRunCmd.Flags().StringVar(&workID, "work-id", "", "The work ID for acknowledging work items from the API")
	taskRunCmd.Flags().BoolVar(&stdin, "stdin", false, "Read session context from stdin instead of using script-path. When enabled, expects JSON on stdin.")
	taskRunCmd.Flags().StringVarP(&outputFile, "output-file", "o", "", "Path to write task output. If not specified, output goes to stdout. When specified, captures script stdout/stderr to this file.")
	taskRunCmd.Flags().StringVarP(&workingDir, "working-dir", "d", "", "Working directory for script execution. Defaults to current directory. The script will be executed with this as its working directory.")
	taskRunCmd.Flags().StringVar(&scriptPath, "script-path", "", "Path to the script to execute. Required unless --stdin is used. The script must be executable.")
	taskRunCmd.Flags().StringVar(&inputFormat, "input-format", "v1", "Input format version for stdin parsing. Use 'v0' for legacy format or 'v1' for the work response format.")
	taskRunCmd.Flags().BoolVar(&sandboxEnabled, "sandbox-enabled", true, "Enable sandbox wrapping for script execution. When enabled, the script runs inside a security sandbox.")
	taskRunCmd.Flags().StringVar(&sandboxCommand, "sandbox-command", "", "Custom sandbox command to use for wrapping script execution. Overrides the default sandbox binary. Supports multiple sandbox backends with configurable security profiles.")
	taskRunCmd.Flags().BoolVar(&debug, "debug", false, "Enable debug mode with verbose logging for troubleshooting")
	taskRunCmd.Flags().BoolVar(&sandboxDisabled, "sandbox-disabled", false, "Explicitly disable sandbox wrapping for script execution. Overrides --sandbox-enabled.")
	taskRunCmd.Flags().StringVar(&sandboxBackend, "sandbox-backend", "", "Sandbox backend to use (e.g., bubblewrap, firecracker)")
	taskRunCmd.Flags().StringVar(&logFile, "log-file", "", "Path to log file")
	taskRunCmd.Flags().StringVar(&secretPath, "secret-path", "", "Path to secret key file")
	taskRunCmd.Flags().StringVar(&logLevel, "log-level", "info", "Log level (debug, info, warn, error)")
	taskRunCmd.Flags().BoolVar(&enableTelemetry, "enable-telemetry", false, "Enable OpenTelemetry reporting")
	taskRunCmd.Flags().BoolVar(&metricsEnabled, "metrics-enabled", false, "Enable metrics collection")
	taskRunCmd.Flags().StringVar(&sessionID, "session-id", "", "The session ID for this task run")
	taskRunCmd.Flags().StringVar(&mode, "session-mode", "", "Session mode (new, resume, setup-only, resume-cached)")
	taskRunCmd.Flags().StringVar(&secretKeyVar, "secret-key", "", "Secret key value for API authentication")

	// Add to root command.
	rootCmd.AddCommand(taskRunCmd)
}

// InputParser is the interface for parsing stdin input data.
// Implementations: V0Parser (legacy), V1Parser (work response format).
//
// Binary itabs:
//   go:itab.*input.V0Parser,input.InputParser at 0xf5a200
//   go:itab.*input.V1Parser,input.InputParser at 0xf5a220
type InputParser interface {
	Parse(data []byte) (*input.ParsedContext, error)
}

// loadContextFromStdin reads and parses session context from stdin.
// It reads all data from os.Stdin, selects the appropriate parser based on
// the inputFormat parameter ("v0" or "v1"), and returns the parsed context.
//
// Binary: 0xb7b1e0 - cmd.loadContextFromStdin
// Source: cmd/cmd_task_run.go
//
// Parameters (register-based ABI):
//   AX = logger (*slog.Logger) - data ptr
//   BX = logger (*slog.Logger) - handler
//   CX = logger (*slog.Logger) - level
//   DI = inputFormat string data
//   SI = inputFormat string len
//   R8 = secretPath string data
//   R9 = secretPath string len
//   R10 = secretKeyVar string data
//   R11 = secretKeyVar string len
//
// Returns:
//   AX = *input.ParsedContext (nil on error)
//   BX = error interface type
//   CX = error interface data
//
// Flow:
//  1. time.Now() for timing
//  2. slog.Info "Starting to read and parse stdin" with 2 attrs (input_format)
//  3. io.ReadAll(os.Stdin) to read all stdin data
//  4. On read error: return fmt.Errorf("failed to read from stdin: %w", err)
//  5. Measure data size and read duration
//  6. slog.Info "Read stdin data" with 4 attrs (input_format, data_size, stdin_parse_duration_ms, total_parse_duration_ms)
//  7. Select parser based on inputFormat:
//     - "v0": create *input.V0Parser{Logger, SessionID, O11y}
//     - "v1": create *input.V1Parser{Logger, SessionID, O11y}
//     - other: return fmt.Errorf("unsupported input format: %q (expected 'v0' or 'v1')", inputFormat)
//  8. Call parser.Parse(data)
//  9. On parse error: return fmt.Errorf("failed to parse stdin: %w", err)
//  10. Measure total duration
//  11. slog.Info "Completed parsing stdin context" with 4 attrs (input_format, format, data_size, total_parse_duration_ms)
//  12. Wrap result in stdinContextResult and return
func loadContextFromStdin(
	slogger *slog.Logger,
	inputFormat string,
	secretPath string,
	secretKeyVar string,
	sessionID string,
) (*input.ParsedContext, error) {
	// 0xb7b245: Record start time
	readStart := time.Now()

	// 0xb7b2d6-0xb7b2f4: Log start message
	// String: "Starting to read and parse stdin" (32 chars)
	slogger.Info("Starting to read and parse stdin",
		"input_format", inputFormat,
	)

	// 0xb7b2f9-0xb7b307: Read all stdin data
	data, err := io.ReadAll(os.Stdin)
	if err != nil {
		// 0xb7b337-0xb7b34c: fmt.Errorf("failed to read from stdin: %w", err)
		return nil, fmt.Errorf("failed to read from stdin: %w", err)
	}

	// 0xb7b362-0xb7b3a9: Compute read duration in milliseconds
	dataSize := len(data)
	readDurationMs := time.Since(readStart).Milliseconds()

	// 0xb7b46a-0xb7b488: Log stdin data read with timing
	// String: "Read stdin data" (15 chars), 4 attrs
	slogger.Info("Read stdin data",
		"data_size", dataSize,
		"input_format", inputFormat,
		"stdin_parse_duration_ms", readDurationMs,
	)

	// 0xb7b495-0xb7b578: Select parser based on input format
	var parser InputParser
	switch inputFormat {
	case "v0":
		// 0xb7b51a-0xb7b571: Create V0Parser
		parser = &input.V0Parser{
			Logger:    slogger,
			SessionID: sessionID,
		}
	case "v1":
		// 0xb7b4b9-0xb7b518: Create V1Parser
		parser = &input.V1Parser{
			Logger:    slogger,
			SessionID: sessionID,
		}
	default:
		// 0xb7b80d-0xb7b863: Unsupported format error
		// String: "unsupported input format: %q (expected 'v0' or 'v1')" (52 chars)
		return nil, fmt.Errorf("unsupported input format: %q (expected 'v0' or 'v1')", inputFormat)
	}

	// 0xb7b578-0xb7b5a0: Call parser.Parse(data)
	parsedCtx, err := parser.Parse(data)
	if err != nil {
		// 0xb7b5cd-0xb7b5e2: fmt.Errorf("failed to parse stdin: %w", err)
		return nil, fmt.Errorf("failed to parse stdin: %w", err)
	}

	// 0xb7b5f8-0xb7b62f: Compute total parse duration
	totalDurationMs := time.Since(readStart).Milliseconds()

	// 0xb7b6f6-0xb7b714: Log completion
	// String: "Completed parsing stdin context" (31 chars), 4 attrs
	slogger.Info("Completed parsing stdin context",
		"input_format", inputFormat,
		"data_size", dataSize,
		"total_parse_duration_ms", totalDurationMs,
	)

	// 0xb7b719-0xb7b804: Wrap parsed context and return
	return parsedCtx, nil
}

// acknowledgeWorkIfNeeded sends a work acknowledgment to the API if a work ID
// is configured and the necessary authentication context is available.
//
// Binary: 0xb7bb20 - cmd.acknowledgeWorkIfNeeded
// Source: cmd/cmd_task_run.go
//
// Parameters (register-based ABI):
//   AX = logger (*slog.Logger) data
//   BX = logger (*slog.Logger) handler
//   CX = secretPath string data
//   DI = session (*config.Session) pointer
//   SI = workID string data
//   R8 = workID string len
//   R9 = envServiceKey string data
//   R10 = envServiceKey string len
//   R11 = hasContext bool
//
// Returns:
//   AX = error interface type
//   BX = error interface data
//
// Flow:
//  1. Early return nil if workID is empty, envServiceKey is empty, or !hasContext
//  2. Check session.StartupContext != nil and session.StartupContext.APIBaseURL != ""
//  3. If startup context missing: return fmt.Errorf("cannot ACK work %s: missing startup context", workID)
//  4. If API base URL empty: return fmt.Errorf("cannot ACK work %s: missing startup context", workID)
//  5. Validate API base URL via validateAPIBaseURL
//  6. If validation fails: return fmt.Errorf("cannot ACK work %s: %w", workID, err)
//  7. Check session auth token present (session.StartupContext.AuthContext != nil)
//  8. If missing: return fmt.Errorf("cannot ACK work %s: empty session ingress token", workID)
//  9. Log "Acknowledging work item" with attrs (work_id, api_url, session_id, has_context)
//  10. Create api.NewHttpClient with API base URL
//  11. Build WorkClient with httpClient and auth token
//  12. Create context with 30s timeout
//  13. Call workClient.AcknowledgeWork(ctx, environmentID, workID)
//  14. On error: cancel context, return fmt.Errorf("failed to acknowledge work: %w", err)
//  15. On success: cancel context, log "Work item acknowledged successfully", return nil
func acknowledgeWorkIfNeeded(
	slogger *slog.Logger,
	apiURL string,
	secretPath string,
	sess *config.Session,
	workID string,
	envServiceKey string,
	hasContext bool,
) error {
	// 0xb7bb60-0xb7bb7b: Early return if no work to acknowledge
	if workID == "" || envServiceKey == "" || !hasContext {
		return nil
	}

	// 0xb7bb7c-0xb7bb87: Check session has startup context
	if sess.StartupContext == nil {
		// 0xb7c066-0xb7c0ba: Missing startup context error
		return fmt.Errorf("cannot ACK work %s: missing auth context", workID)
	}

	// 0xb7bb87-0xb7bb93: Check startup context has API base URL
	apiBaseURL := sess.StartupContext.APIBaseURL
	if apiBaseURL == "" {
		// 0xb7c011-0xb7c065: Missing API URL error
		return fmt.Errorf("cannot ACK work %s: missing startup context", workID)
	}

	// 0xb7bbe0: Validate API base URL
	if err := validateAPIBaseURL(apiBaseURL); err != nil {
		// 0xb7bf77-0xb7c010: Validation error
		return fmt.Errorf("cannot ACK work %s: %w", workID, err)
	}

	// 0xb7bbee-0xb7bc00: Check auth token is present
	// Reads session ingress token from auth context at offset 0x10
	if sess.StartupContext == nil {
		// 0xb7bf18-0xb7bf76: Missing auth context
		return fmt.Errorf("cannot ACK work %s: empty session ingress token", workID)
	}

	// 0xb7bc06-0xb7bd40: Log acknowledgment attempt with details
	// String: "Acknowledging work item" (23 chars), 6 attrs
	slogger.Info("Acknowledging work item",
		"work_id", workID,
		"api_url", apiBaseURL,
		"session_id", sess.SessionID,
		"has_context", hasContext,
	)

	// 0xb7bd50-0xb7bd62: Create HTTP client
	httpClient := api.NewHttpClient(apiBaseURL, secretPath, envServiceKey, nil)

	// 0xb7bd67-0xb7bd9c: Build WorkClient
	workClient := &api.WorkClient{
		Client: httpClient,
	}

	// 0xb7bdbc-0xb7bdf6: Create context with 30s timeout (0x6fc23ac00 ns)
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)

	// 0xb7be00: Call AcknowledgeWork
	err := workClient.AcknowledgeWork(ctx, sess.SessionID, workID)
	if err != nil {
		// 0xb7be0a-0xb7be73: Handle error, cancel context, return wrapped error
		cancel()
		return fmt.Errorf("failed to acknowledge work: %w", err)
	}

	// 0xb7be74-0xb7bf17: Success path
	cancel()

	// Log success
	// String: "Work item acknowledged successfully" (35 chars), 2 attrs
	slogger.Info("Work item acknowledged successfully",
		"work_id", workID,
	)

	return nil
}

// stdinConfigClient implements the api.Client interface by reading
// session configuration from stdin-parsed input data. It wraps the
// ParsedContext returned by loadContextFromStdin and provides accessor
// methods for the environment config, auth context, and outcomes.
//
// Struct layout (from GetEnvironmentForSession disassembly):
//   offset 0x00: *input.ParsedContext - main parsed data
//   offset 0x08: *auth.AuthContext    - extracted auth context
//   offset 0x10: *claude.Outcomes     - extracted outcomes
//   offset 0x18: *slog.Logger         - logger instance
//
// Binary itab: go:itab.*cmd.stdinConfigClient,api.Client at 0xf5a240
type stdinConfigClient struct {
	parsedCtx        *input.ParsedContext // offset 0x00
	authCtx          *auth.AuthContext    // offset 0x08
	outcomes         *claude.Outcomes     // offset 0x10
	logger           *slog.Logger         // offset 0x18
	activityRecorder interface{}          // offset 0x20 - session.ActivityRecorder interface
}

// GetEnvironmentForSession returns the environment configuration from the
// stdin-parsed data. It logs the configuration details including the session ID,
// API base URL, and whether various config elements are present.
//
// Binary: 0xb7b8e0 - (*stdinConfigClient).GetEnvironmentForSession
// Source: cmd/cmd_task_run.go
//
// Parameters (register-based ABI):
//   AX = *stdinConfigClient (receiver)
//   BX, CX = logger context (handler, level)
//   DI = context (for slog)
//   SI = session ID string data / len
//
// Returns:
//   AX = *input.ParsedContext.EnvironmentConfig (the first field of ParsedContext, JSON raw message)
//   BX = error interface type (always nil)
//   CX = error interface data (always nil)
//
// Flow:
//  1. Read logger from offset 0x18
//  2. Read ParsedContext from offset 0x00
//  3. Check if ParsedContext has AllowedTools (offset 0x30 != nil -> bool)
//  4. Log "Using stdin-provided environment configuration" (46 chars) at Info level
//     with 8 attrs: session_id (2x), api_base_url (2x), has_allowed_tools (bool),
//     environment_sub_type
//  5. Return ParsedContext.EnvironmentConfig (offset 0x00 of ParsedContext)
func (s *stdinConfigClient) GetEnvironmentForSession(ctx context.Context, sessionID string) (json.RawMessage, error) {
	slogger := s.logger
	parsedCtx := s.parsedCtx

	// Check if startup context has allowed tools
	hasAllowedTools := false
	if parsedCtx != nil && parsedCtx.StartupContext != nil {
		hasAllowedTools = len(parsedCtx.StartupContext.AllowedTools) > 0
	}

	// Get API base URL and environment sub type from startup context
	var apiBaseURL string
	var envSubType string
	if parsedCtx != nil && parsedCtx.StartupContext != nil {
		apiBaseURL = parsedCtx.StartupContext.APIBaseURL
		envSubType = parsedCtx.StartupContext.EnvironmentSubType
	}

	// 0xb7ba64-0xb7ba80: Log with 8 attrs
	// String: "Using stdin-provided environment configuration" (46 chars)
	slogger.Info("Using stdin-provided environment configuration",
		"session_id", sessionID,
		"api_base_url", apiBaseURL,
		"has_allowed_tools", hasAllowedTools,
		"environment_sub_type", envSubType,
	)

	// 0xb7ba8d-0xb7ba9c: Return environment config from parsed context
	if parsedCtx != nil {
		return parsedCtx.EnvironmentConfig, nil
	}
	return nil, nil
}

// GetAuthContext returns the auth context from the stdin-parsed data.
// This is a simple field accessor that returns the AuthContext stored
// at offset 0x08 of the stdinConfigClient.
//
// Binary: 0xb7bae0 - (*stdinConfigClient).GetAuthContext
// Source: cmd/cmd_task_run.go
//
// Disassembly (2 instructions):
//   0xb7bae0  MOVQ 0x8(AX), AX    ; load field at offset 0x08
//   0xb7bae4  RET
func (s *stdinConfigClient) GetAuthContext() *auth.AuthContext {
	// 0xb7bae0: MOVQ 0x8(AX), AX - return field at offset 0x08
	return s.authCtx
}

// GetOutcomes returns the outcomes from the stdin-parsed data.
// This is a simple field accessor that returns the Outcomes stored
// at offset 0x10 of the stdinConfigClient.
//
// Binary: 0xb7bb00 - (*stdinConfigClient).GetOutcomes
// Source: cmd/cmd_task_run.go
//
// Disassembly (2 instructions):
//   0xb7bb00  MOVQ 0x10(AX), AX   ; load field at offset 0x10
//   0xb7bb04  RET
func (s *stdinConfigClient) GetOutcomes() *claude.Outcomes {
	// 0xb7bb00: MOVQ 0x10(AX), AX - return field at offset 0x10
	return s.outcomes
}

// initDiagLogging initializes the diagnostic logging service for a session.
// If diagnostic logging is disabled (diag.LogsEnabled() returns false), it logs
// a message and returns a no-op cleanup function. Otherwise, it creates an HTTP
// client, a SessionIngressLogFlusher, and a DiagService.
//
// Binary: 0xb7c260 - cmd.initDiagLogging
// Source: cmd/cmd_task_run.go
//
// DWARF-verified parameters (line 710-715):
//
//	ctx                  context.Context   (AX=type, BX=value)
//	apiBaseURL           string            (CX=ptr, DI=len)
//	sessionIngressToken  string            (SI=ptr, R8=len)
//	sessionID            string            (R9=ptr, R10=len)
//	logger               *slog.Logger      (R11)
//
// Flow:
//  1. Call diag.LogsEnabled() - checks if diagnostic logging is enabled
//  2. If disabled:
//     - Log "Diagnostic logs are disabled" at info level
//     - Return nil, nil, nil
//  3. If enabled:
//     - Create api.NewHttpClient(apiBaseURL)
//     - Wrap in HttpSessionIngressClient{Client: httpClient, ApiKey: sessionIngressToken, Logger: logger, UseV2: false}
//     - Create SessionIngressLogFlusher{Client: ingressClient, SessionID: sessionID}
//     - Call diag.NewDiagService(ctx, sessionID, ctx, flusher)
//     - If error: return wrapped "failed to initialize diagnostic logging service: %w"
//     - Create cleanup function (func2) that calls DiagService.Shutdown with 10s timeout
//     - Return diagService, cleanup, nil
func initDiagLogging(
	ctx context.Context,
	apiBaseURL string,
	sessionIngressToken string,
	sessionID string,
	logger *slog.Logger,
) (*diag.DiagService, func(), error) {
	if !diag.LogsEnabled() {
		logger.Info("Diagnostic logs are disabled")
		return nil, nil, nil
	}

	httpClient := api.NewHttpClient(apiBaseURL)

	ingressClient := &api.HttpSessionIngressClient{
		Client: httpClient,
		ApiKey: sessionIngressToken,
		Logger: logger,
	}

	flusher := &diag.SessionIngressLogFlusher{
		Client:    ingressClient,
		SessionID: sessionID,
	}

	diagService, err, _ := diag.NewDiagService(ctx, sessionID, ctx, flusher)
	if err != nil {
		return nil, nil, fmt.Errorf("failed to initialize diagnostic logging service: %w", err)
	}

	// Cleanup function that shuts down the diagnostic service with a 10s timeout.
	// Binary: 0xb7c580 - cmd.initDiagLogging.func2
	// Uses context.WithTimeout with 10s (0x2540be400 ns = 10,000,000,000 ns)
	// Calls diagService.Shutdown(ctx)
	// On error: logs at slog.LevelError (8):
	//   "Failed to shutdown diagnostic logging service" (0x2d = 45 chars)
	// Then calls the cancel function from WithTimeout
	cleanup := func() {
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
		defer cancel()

		diagService.Shutdown(ctx, sessionID)
	}

	return diagService, cleanup, nil
}
