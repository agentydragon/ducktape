// Reconstructed from binary: environment-manager (Build ID 64bc4dc1)
// Source: internal/claude/claude_code_executor.go
// Original path: /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/claude/claude_code_executor.go

package claude

import (
	"context"
	"encoding/base64"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/exec"
	"strings"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/config"
)

// Executor is the interface for executing Claude Code. ClaudeCodeExecutor
// implements this interface (confirmed by itab in binary).
type Executor interface {
	Execute(ctx context.Context) error
	Destroy(ctx context.Context) error
	SetClaudePath(path string)
}

// GatewayConfig holds gateway configuration including process count, ClaudeCodeArgs,
// and McpConfig. Not fully reconstructed — field access patterns visible in
// buildArgsFromGatewayConfig (offsets 0xc0=ClaudeCodeArgs, 0xc8=McpConfig).
type GatewayConfig struct{}

// SessionIngressClient is the HTTP session ingress client used for WebSocket
// tunnel connections. Referenced as *api.HttpSessionIngressClient in the binary.
type SessionIngressClient struct{}

// OutcomesExtra holds additional outcomes-related data (concrete fields not recovered).
type OutcomesExtra struct{}

// DiagReporter handles diagnostics reporting. Nil-checked with panic
// "diagReporter must not be nil" in NewClaudeCodeExecutor.
// Provides GetClaudeCodeDiagFilePath() for CLAUDE_CODE_DIAGNOSTICS_FILE env var.
type DiagReporter struct{}

// ClaudeCodeExecutor manages the lifecycle of a Claude Code process.
//
// Struct layout (from NewClaudeCodeExecutor at 0xaef8a0, size ~0x98 = 152 bytes):
//
//	Offset 0x00: Logger          *slog.Logger           — from AX param (line 134)
//	Offset 0x08: LoggerName      string                 — from 0x100(SP) param (line 135)
//	Offset 0x10: Ctx             context.Context        — interface{itab,ptr}: DI→0x18, value→0x10 (line 136)
//	Offset 0x20: ClaudePathCmd   string                 — from R8 (GetClaudePath result) (line 137)
//	Offset 0x30: Config          *config.StartupContext — from SI param (line 139)
//	Offset 0x38: GatewayConfig   *GatewayConfig         — from R12 param (line 139/140)
//	Offset 0x40: WorkingDir      string                 — from R9 param or default "/home/claude/project" (line 138)
//	Offset 0x50: Outcomes        *Outcomes              — from 0xc0(SP) (line 142)
//	Offset 0x58: SessionIngress  *SessionIngressClient  — from R13 param (line 141)
//	Offset 0x60: SessionToken    string                 — from 0x130(SP)/0x138(SP) (line 141)
//	Offset 0x70: OutcomesExtra   *OutcomesExtra         — from 0xc8(SP) (line 143)
//	Offset 0x80: DiagReporter    *DiagReporter          — from 0xd0(SP) (line 144)
//	Offset 0x88: ClaudePath      string                 — from 0xd8(SP)/0xe0(SP) (line 145)
//
// Struct field types recovered from NewClaudeCodeExecutor (0xaef8a0) register assignments
// and struct field store sequence at lines 134-145 of the original source.
//
// Register-to-field mapping in NewClaudeCodeExecutor:
//
//	AX(logger) → offset 0x00   BX(ctx itab) → offset 0x18
//	DI(ctx val) → offset 0x10  SI(config) → offset 0x30
//	CX(loggerName) → offset 0x08  R8(claudePath) → offset 0x20
//	R9(workingDir data) → offset 0x40  R10(gatewayConfig) → offset 0x30+adj
//	R12(sessionIngress) → offset 0x38  R13(sessionToken) → offset 0x58
//	0xc0(SP)(outcomes) → offset 0x50  0xc8(SP)(outcomes extra) → offset 0x78
//	0xd0(SP)(diagReporter) → offset 0x80  0xd8(SP)/0xe0(SP) → offset 0x88/0x90
type ClaudeCodeExecutor struct {
	Logger         *slog.Logger           // offset 0x00 — from AX param, stored at 0(result)
	LoggerName     string                 // offset 0x08 — logger name string (line 135)
	Ctx            context.Context        // offset 0x10 — context (itab at 0x18, val at 0x10); from DI param
	ClaudePathCmd  string                 // offset 0x20 — claude binary command path (set by SetClaudePath)
	Config         *config.StartupContext // offset 0x30 — startup context from work payload; from SI param
	GatewayConfig  *GatewayConfig         // offset 0x38 — gateway configuration; from R12 param (line 139)
	WorkingDir     string                 // offset 0x40 — working directory (data + len)
	Outcomes       *Outcomes              // offset 0x50 — outcomes recorder; from 0xc0(SP) (line 142)
	SessionIngress *SessionIngressClient  // offset 0x58 — session ingress client; from R13 param (line 141)
	SessionToken   string                 // offset 0x60 — session token string
	OutcomesExtra  *OutcomesExtra         // offset 0x70 — additional outcomes data; from 0xc8(SP) (line 143)
	DiagReporter   *DiagReporter          // offset 0x80 — diagnostics reporter; from 0xd0(SP) (line 144), nil-checked with panic "diagReporter must not be nil"
	ClaudePath     string                 // offset 0x88 — resolved claude path (from GetClaudePath)
}

// NewClaudeCodeExecutor creates a new ClaudeCodeExecutor from the provided
// parameters. It validates that required parameters (logger, config, etc.)
// are non-nil, panicking if any are missing. It resolves the Claude binary
// path via GetClaudePath and stores it in the executor.
//
// The function accepts ~10 parameters via registers (AX through R11) matching
// the Go ABI internal calling convention.
//
// Panics:
//   - "logger must not be nil" if AX (logger) is nil (0xad8c65)
//   - "ctx must not be nil" if DI (ctx) is nil (0xad8c4e)
//   - "config must not be nil" if SI (config) is nil (0xad8c3b)
//   - "outcomes must not be nil" if 0xc0(SP) is nil (0xad8c28)
//   - "diagReporter must not be nil" if 0xd0(SP) is nil (0xad8c15)
//
// Binary address: 0xad8800 - 0xad8ce4
func NewClaudeCodeExecutor(
	logger *slog.Logger,
	ctx context.Context,
	cfg *config.StartupContext,
	outcomes *Outcomes,
	diagReporter *DiagReporter,
	// additional parameters from stack: gatewayConfig, sessionIngress, sessionToken, etc.
) *ClaudeCodeExecutor {
	if logger == nil {
		panic("logger must not be nil")
	}
	if ctx == nil {
		panic("ctx must not be nil")
	}
	if cfg == nil {
		panic("config must not be nil")
	}
	if outcomes == nil {
		panic("outcomes must not be nil")
	}
	if diagReporter == nil {
		panic("diagReporter must not be nil")
	}

	// 0xad88b3: config.WorkingDir check — if config.WorkingDir (offset 0x20) is empty,
	// use default "/home/user/project" (len 0x11 = 17)
	// 0xad88ba-0xad88c7: default WorkingDir string load

	// 0xad88cc-0xad8956: slog.Info
	// Message length 0x25 = 37: "Constructing new Claude code executor"
	logger.InfoContext(ctx,
		"Constructing new Claude code executor",
		"claudeConfig", cfg,
	)

	// 0xad89a0: call GetClaudePath to resolve the binary
	claudePath, _ := GetClaudePath(logger, ctx, nil)

	// 0xad89c0-0xad8a6c: check GatewayConfig for process count, log it
	// Message length 0x37 = 55:
	// "Constructing new Claude code executor with gateway config"

	// 0xad8a71-0xad8c0c: allocate ClaudeCodeExecutor via runtime.newobject
	// and populate all fields from parameters
	executor := &ClaudeCodeExecutor{
		Logger:     logger,
		ClaudePath: claudePath,
		Config:     cfg,
		Outcomes:   outcomes,
	}

	return executor
}

// addTokenViaFileDescriptor creates an os.Pipe, writes the session ingress
// token to the write end, closes the write end, and appends the read end's
// file descriptor to the executor's ExtraFiles and Args slices.
//
// This allows passing the token to the Claude Code process securely via a
// file descriptor rather than a command-line argument or environment variable.
//
// Parameters are passed via registers:
//
//	AX: executor (*ClaudeCodeExecutor)
//	BX, CX: logger (interface)
//	DI, SI: token (string)
//	R8, R9: key name (string, e.g. "session_ingress_token")
//	R10, R11: cmd ExtraFiles and Args slices
//
// Returns:
//
//	AX: next fd number (int), or 0 on error
//	BX, CX: error (interface)
//
// Binary address: 0xad8d00 - 0xad9460
func addTokenViaFileDescriptor(
	logger *slog.Logger,
	ctx context.Context,
	token string,
	keyName string,
	cmdExtraFiles *[]*os.File,
	cmdArgs *[]string,
) (int, error) {
	// 0xad8d65: os.Pipe()
	readFile, writeFile, err := os.Pipe()
	if err != nil {
		// 0xad8d73-0xad8e06: fmt.Errorf
		// Format length 0x20 = 32: "failed to create pipe for %s: %w"
		return 0, fmt.Errorf("failed to create pipe for %s: %w", keyName, err)
	}

	// 0xad8e1c-0xad8e57: concat token + "\n"
	data := token + "\n"

	// 0xad8e60-0xad8e76: convert to []byte and write to writeFile
	_, writeErr := writeFile.Write([]byte(data))

	// 0xad8e93-0xad8eab: close writeFile
	closeErr := writeFile.Close()

	// Combine errors — check write error
	if writeErr != nil {
		// 0xad8f14-0xad8f97: fmt.Sprintf
		// Format length 0x24 = 36: "failed to write to pipe for key %s"
		msg := fmt.Sprintf("failed to write to pipe for key %s", keyName)

		// 0xad8fa1-0xae0fc2: slog.Warn (level 8)
		logger.Warn(msg,
			"error", writeErr,
		)
	}

	// Check close error on read side
	if closeErr != nil {
		// 0xad9072-0xad90e8: fmt.Sprintf
		// Format length 0x23 = 35: "failed to close pipe for key %s"
		msg := fmt.Sprintf("failed to close pipe for key %s", keyName)

		// 0xad90f4-0xad9122: slog.Error (level -4)
		logger.Error(msg,
			"error", closeErr,
		)
	}

	// 0xad9127-0xad91af: fmt.Errorf wrapping both errors
	if writeErr != nil || closeErr != nil {
		// 0xad9193-0xad91af: fmt.Errorf
		// Format length 0x1e = 30: "pipe errors for key %s: %v, %v"
		return 0, fmt.Errorf("pipe errors for key %s: %v, %v", keyName, writeErr, closeErr)
	}

	// 0xad91c5-0xad9252: append readFile to cmdExtraFiles
	*cmdExtraFiles = append(*cmdExtraFiles, readFile)

	// 0xad925a-0xad92d6: append readFile to cmdArgs
	*cmdArgs = append(*cmdArgs, readFile.Name())

	// 0xad92db-0xad92e3: compute fd number (len(extraFiles) + 2)
	fdNum := len(*cmdExtraFiles) + 2

	// 0xad92f4-0xad93e4: log info
	// Format length 0x1d = 29: "added token via fd for key %s"
	tokenLog := fmt.Sprintf("added token via fd for key %s", keyName)

	logger.Info(tokenLog,
		"fdNum", fdNum,
	)

	return fdNum, nil
}

// Execute runs the Claude Code process. This is the largest function in the
// package (~25KB of machine code), handling:
//   - Building command-line arguments from gateway config
//   - Setting up environment variables (GetClaudeEnvironmentVariables)
//   - Creating the exec.Cmd with proper stdin/stdout/stderr
//   - Setting up file descriptors for token passing (addTokenViaFileDescriptor)
//   - Running the process and capturing output via io.MultiWriter
//   - Handling process exit codes and errors
//   - Writing diagnostics and outcomes
//   - Printing code logs on failure (printCodeLogs)
//   - Sending SIGTERM on context cancellation (via goroutine closure func1)
//
// The function spawns several goroutines via closures:
//
// Execute.func1 (0xadf400): Grace period signal sender. Reads the grace period
//
//	from a LeaseManager, converts duration to float64 seconds, sends SIGTERM to
//	the Claude Code process. If signal fails, logs a WARN. Called when the
//	pod monitor indicates the lease is expiring.
//
// Execute.func2 (0xadf2a0): File descriptor cleanup closure. Iterates over all
//
//	ExtraFiles on the exec.Cmd and closes each one. If close fails, logs a
//	DEBUG message "failed to close extra file" (len 0x1e = 30).
//
// Execute.func3 (0xadf1c0): Write-end pipe cleanup. Closes a single *os.File
//
//	(the write end of a pipe from addTokenViaFileDescriptor). If close fails,
//	logs WARN "failed to close pipe" (len 0x18 = 24).
//
// Execute.func4 (0xadf0a0): Output writer closure. Writes buffered output data
//
//	to a file. If the write fails, logs WARN "failed to write output to file"
//	(len 0x22 = 34).
//
// Binary address: 0xad9480 - 0xadf6a0
func (e *ClaudeCodeExecutor) Execute(ctx context.Context) error {
	// 0xad94db-0xad9518: slog.Info
	// First log message at offset 0x47b24f with "claudeConfig" attribute
	e.Logger.InfoContext(ctx,
		"Executing Claude Code",
		"claudeConfig", e.Config, // *config.StartupContext
	)

	// 0xad9540: Set up environment variables
	// Get base environment from os.Environ()
	envVars := os.Environ()

	// The executor appends env vars to the process environment in this order
	// (from the original source lines 202-412 of the old binary's Execute method):
	//
	// 1. Base CLI args (not env vars): --output-format=stream-json, --verbose,
	//    --replay-user-messages, --input-format=stream-json, --debug-to-stderr (lines 202-207)
	//
	// 2. buildArgsFromGatewayConfig: builds --key=value args from ClaudeCodeArgs map (line 216)
	//
	// 3. buildSessionURLs: constructs session ingress URL and WebSocket URL (line 275)
	//
	// 4. --resume=%s arg with session ingress URL (line 280)
	//
	// 5. slog.Info with attributes: claudeConfig, debug, sessionURL, wsURL, args, loggerName (lines 282-288)
	//
	// 6. CLAUDE_CODE_DEBUG: read from os.Getenv("CLAUDE_CODE_DEBUG"), appended to args if set (line 294)
	//
	// 7. Env vars appended to the cmd.Env slice:
	//   ANTHROPIC_BASE_URL=%s                        — from e.Config.APIBaseURL (line 399)
	//   CLAUDE_CODE_SESSION_ID=%s                     — from e.Config.SessionID via e.Config offset 0x10 (line 404)
	//   CLAUDE_CODE_REMOTE_SESSION_ID=%s              — from e.Config.SessionID via e.Config offset 0x10 (line 405)
	//   CLAUDE_CODE_ENVIRONMENT_RUNNER_VERSION=%s     — from util.Version (line 408)
	//   CLAUDE_CODE_DIAGNOSTICS_FILE=%s               — from diag.GetClaudeCodeDiagFilePath (line 412)
	//
	// 8. addTokenViaFileDescriptor calls for fd-based token passing:
	//   CLAUDE_CODE_WEBSOCKET_AUTH_FILE_DESCRIPTOR=%d — fd number for WebSocket auth (line 461)
	//   CLAUDE_SESSION_INGRESS_TOKEN_FILE=%s          — path to token file (line 482)
	//
	// 9. The session ingress token is also written to a file at
	//    /home/claude/.claude/remote/.session_ingress_token (lines 468-476)
	//
	// 10. filterInitOnlyFromEnviron: filters the env var list (line 380)
	//
	// 11. Config-sourced env vars from EnvironmentVariables map (line 385):
	//     Iterates e.Config.EnvironmentVariables and appends key=value pairs.
	//     This is where CLAUDE_CODE_USE_CCR_V2, CLAUDE_CODE_WORKER_EPOCH,
	//     CLAUDE_CODE_STUCK_THRESHOLD_SECONDS, and CLAUDE_CODE_EXIT_AFTER_STOP_DELAY
	//     come from — they are injected by the orchestrator into the work payload's
	//     environment_variables map, not set directly by the executor.
	//
	// Evidence for #11:
	//   - CLAUDE_CODE_USE_CCR_V2: constant string in binary, no executor logic to compute it
	//   - CLAUDE_CODE_WORKER_EPOCH: WorkerEpoch is an int64 (json:"worker_epoch,string") on a
	//     separate struct used for URL construction (%s?worker_epoch=%d), not for env var setting
	//   - CLAUDE_CODE_STUCK_THRESHOLD_SECONDS: "invalid CLAUDE_CODE_STUCK_THRESHOLD_SECONDS, using
	//     default" string exists in binary — read from env by the CC process itself, not set by executor
	//   - CLAUDE_CODE_EXIT_AFTER_STOP_DELAY: same pattern as above

	// Inject environment variables from the work payload
	if e.Config != nil {
		for key, value := range e.Config.EnvironmentVariables {
			envVars = append(envVars, key+"="+value)
		}
	}

	// Set ANTHROPIC_BASE_URL from config
	if e.Config != nil && e.Config.APIBaseURL != "" {
		envVars = append(envVars, fmt.Sprintf("ANTHROPIC_BASE_URL=%s", e.Config.APIBaseURL))
	}

	// Set session ID env vars from config
	if e.Config != nil && e.Config.SessionID != "" {
		envVars = append(envVars, fmt.Sprintf("CLAUDE_CODE_SESSION_ID=%s", e.Config.SessionID))
		envVars = append(envVars, fmt.Sprintf("CLAUDE_CODE_REMOTE_SESSION_ID=%s", e.Config.SessionID))
	}

	// Set runner version from util.Version
	// envVars = append(envVars, fmt.Sprintf("CLAUDE_CODE_ENVIRONMENT_RUNNER_VERSION=%s", util.Version))

	// Build command arguments
	// 0xad9620-0xad96e0: start building args slice
	// Base args include the claude binary path and subcommands

	// 0xad97f0-0xad9890: buildArgsFromGatewayConfig if gateway config present
	args, err := e.buildArgsFromGatewayConfig(ctx)
	if err != nil {
		return err
	}

	// 0xad9920-0xad9a20: addTokenViaFileDescriptor for session_ingress_token
	// Passes the session ingress token via file descriptor

	// 0xad9a80-0xad9c00: set up exec.Cmd
	claudePath := e.ClaudePath
	if claudePath == "" {
		claudePath = "claude"
	}

	cmd := exec.CommandContext(ctx, claudePath, args...)

	// 0xad9c50-0xad9cc0: configure cmd.Dir, cmd.Env, cmd.Stdin
	cmd.Dir = e.WorkingDir
	cmd.Env = envVars
	cmd.Stdin = os.Stdin

	// 0xad9d00-0xad9d80: set up stdout/stderr as io.MultiWriter
	// Creates io.MultiWriter for both os.Stdout/Stderr and internal buffers
	// The internal buffers are used for output capture and logging
	cmd.Stdout = io.MultiWriter(os.Stdout)
	cmd.Stderr = io.MultiWriter(os.Stderr)

	// 0xad9e00-0xad9f00: start the process
	if err := cmd.Start(); err != nil {
		return fmt.Errorf("failed to start claude code: %w", err)
	}

	// Register func1 closure: grace period SIGTERM sender
	// 0xadf400: reads LeaseManager.GetGracePeriod(), converts to seconds,
	// calls os.Process.signal(syscall.SIGTERM)

	// Register func2 closure: deferred ExtraFiles cleanup
	// 0xadf2a0: iterates cmd.ExtraFiles, closes each, logs errors at DEBUG level
	defer func() {
		for _, f := range cmd.ExtraFiles {
			if f != nil {
				if err := f.Close(); err != nil {
					e.Logger.Debug(
						"failed to close extra file",
						"error", err,
					)
				}
			}
		}
	}()

	// Execute.func3 (0xaf62e0, original source lines 547-551):
	// Deferred closure that closes the CC log file opened at line 543 via os.OpenFile.
	// The log file is opened at /tmp/claude-code.log (0x14=20 chars) with flags 0x441
	// (O_WRONLY|O_CREATE|O_APPEND) and mode 0600. If close fails, logs WARN
	// "Failed to close log file" (24 chars) with "error" attribute.
	// The closure captures: logFile *os.File, executor *ClaudeCodeExecutor, ctx context.Context.
	//
	// Deferred via runtime.deferprocStack at 0xaf2853.
	logFile, logErr := os.OpenFile("/tmp/claude-code.log", os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if logErr != nil {
		return fmt.Errorf("failed to open claude code log file: %w", logErr)
	}
	defer func() {
		if err := logFile.Close(); err != nil {
			e.Logger.Warn(
				"Failed to close log file",
				"error", err,
			)
		}
	}()

	// Execute.func4 (0xaf5fa0, original source lines 670-684):
	// Output tailer goroutine. Receives log entries from a channel (via runtime.chanrecv2
	// at line 671) connected to a util.OutputStreamer/Tailer. For each received entry:
	//   - If the entry has no error (line 672 nil check), logs INFO "cc_log" (6 chars)
	//     with 6 slog attributes: the log line content, logger name, and session context.
	//   - If the entry has an error, logs WARN "CC log tailer error" (19 chars) with
	//     6 slog attributes: error details, file path, and session context.
	// The loop exits when the channel is closed (chanrecv2 returns false at line 671).
	// Returns at line 677 (normal) or line 684 (channel closed).
	//
	// The closure captures: *sync.WaitGroup, context.Context, *slog.Logger,
	// io.ReadCloser, util.OutputStreamer (from the struct type in binary strings).

	// 0xada000-0xadb000: wait for process completion
	waitErr := cmd.Wait()

	// 0xadb100-0xadc000: handle exit code
	if waitErr != nil {
		// Extract exit code, log error, write diagnostics
		e.Logger.WarnContext(ctx,
			"Claude Code process exited with error",
			"error", waitErr,
		)

		// 0xadc200-0xadd000: print code logs on failure
		e.printCodeLogs(ctx)

		return waitErr
	}

	// 0xadd100: success path
	e.Logger.InfoContext(ctx,
		"Claude Code process completed successfully",
	)

	return nil
}

// buildArgsFromGatewayConfig constructs CLI arguments from the gateway
// configuration. It reads gateway config fields, optionally writes an MCP
// config file, and iterates over ClaudeCodeArgs map entries to build the
// argument list.
//
// The function:
//  1. Checks gateway config McpConfig field (offset 0xc8 in config)
//  2. Reads ClaudeCodeArgs map (offset 0xc0 in config)
//  3. Logs the number of gateway args and whether MCP config is present
//  4. If McpConfig is present, calls writeMCPConfigFileFromGateway
//  5. Iterates ClaudeCodeArgs map and builds "--key=value" style arguments
//
// Binary address: 0xadf6c0 - 0xadfe96
func (e *ClaudeCodeExecutor) buildArgsFromGatewayConfig(ctx context.Context) ([]string, error) {
	if e.Config == nil {
		// No startup context, return empty args
		return []string{}, nil
	}

	// 0xadf709-0xadf718: read ClaudeCodeArgs from config (offset 0xc0)
	// ClaudeCodeArgs is map[string]string (confirmed by binary struct tags: json:"claude_code_args,omitempty")
	claudeCodeArgs := e.Config.ClaudeCodeArgs

	// 0xadf738-0xadf743: check McpConfig != nil (offset 0xc8)
	hasMcpConfig := e.Config.McpConfig != nil

	// 0xadf76b-0xadf828: slog.Info
	// Message length 0x31 = 49:
	// "Building args from gateway config for Claude Code"
	e.Logger.InfoContext(ctx,
		"Building args from gateway config for Claude Code",
		"numGatewayArgs", len(claudeCodeArgs),
		"hasMcpConfig", hasMcpConfig,
	)

	// 0xadf839-0xadf858: if McpConfig present, write MCP config file
	if hasMcpConfig {
		if err := e.writeMCPConfigFileFromGateway(ctx); err != nil {
			// 0xadf8b5-0xadf8f2: fmt.Errorf
			// Format length 0x2b = 43:
			// "failed to write MCP config file from gateway: %w"
			return nil, fmt.Errorf("failed to write MCP config file from gateway: %w", err)
		}
	}

	// 0xadf85d-0xadf93e: iterate ClaudeCodeArgs map
	var args []string

	// 0xadf93e-0xadfe80: map iteration loop
	// For each key-value pair, build "--key=value" style arguments
	for key, value := range claudeCodeArgs {
		// Build argument in "--key=value" format
		arg := fmt.Sprintf("--%s=%s", key, value)
		args = append(args, arg)
	}

	return args, nil
}

// writeMCPConfigFileFromGateway decodes the base64-encoded MCP configuration
// from the gateway config and writes it to a temporary file. The file path
// is then used as a CLI argument for Claude Code.
//
// The function:
//  1. Reads McpConfig from gateway config (offset 0xc8)
//  2. Base64-decodes the config content (offset 0x10/0x18 in McpConfig)
//  3. Determines file path from McpConfig (default "/tmp/mcp_config.json", len 0x14)
//  4. Determines file permissions (default 0x180 = 384 = 0600, capped at 0x1fe = 510)
//  5. Writes decoded content to file via os.WriteFile
//  6. Logs the result
//
// Binary address: 0xadfee0 - 0xae01c1
func (e *ClaudeCodeExecutor) writeMCPConfigFileFromGateway(ctx context.Context) error {
	if e.Config == nil {
		return fmt.Errorf("config is nil")
	}

	// Check if McpConfigFile is present
	if e.Config.McpConfigFile == nil {
		return fmt.Errorf("no MCP config file specified")
	}

	mcpConfigFile := e.Config.McpConfigFile

	// 0xadff0e: read McpConfig (offset 0xc8 of gateway config)
	// 0xadff3b-0xadff4a: base64.StdEncoding.DecodeString
	decoded, err := base64.StdEncoding.DecodeString(mcpConfigFile.Content)
	if err != nil {
		// 0xadff73-0xadff8f: fmt.Errorf
		// Format length 0x27 = 39: "failed to decode MCP config from base64: %w"
		return fmt.Errorf("failed to decode MCP config from base64: %w", err)
	}

	// 0xadffb1-0xadffe9: determine file path and permissions
	// Default path: "/tmp/mcp_config.json" (len 0x14 = 20)
	// Default permissions: 0600 (0x180), capped at 0x1fe (510)
	filePath := mcpConfigFile.Path
	if filePath == "" {
		filePath = "/tmp/mcp_config.json"
	}

	filePerms := os.FileMode(mcpConfigFile.Mode)
	if filePerms == 0 {
		filePerms = 0o600
	}

	// 0xae0002: os.WriteFile
	if err := os.WriteFile(filePath, decoded, filePerms); err != nil {
		// 0xae007a-0xae0096: fmt.Errorf
		// Format length 0x24 = 36: "failed to write MCP config to %s: %w"
		return fmt.Errorf("failed to write MCP config to %s: %w", filePath, err)
	}

	// 0xae00ae-0xae01af: slog.Info
	// Message: "Wrote MCP config file from gateway"
	e.Logger.InfoContext(ctx,
		"Wrote MCP config file from gateway",
		"filePath", filePath,
		"size", len(decoded),
	)

	return nil
}

// Destroy cleans up resources associated with the executor, specifically
// removing the temporary MCP config file if it was created.
//
// The function:
//  1. Logs "Destroying Claude code executor" with the working directory
//  2. Calls os.Remove on the MCP config file path ("/tmp/mcp_config.json", len 0x32 = 50 chars)
//  3. If removal fails and the error is NOT os.ErrNotExist, logs a warning
//  4. If removal fails with a real error, logs the file path and error
//
// Binary address: 0xae0200 - 0xae0466
func (e *ClaudeCodeExecutor) Destroy(ctx context.Context) error {
	// 0xae024c-0xae02c6: slog.Info
	// Message length 0x1d = 29: "Destroying Claude code executor"
	// (actually 29 chars)
	e.Logger.InfoContext(ctx,
		"Destroying Claude code executor",
		"workingDir", e.WorkingDir,
	)

	// 0xae02cb-0xae02d7: os.Remove with path length 0x32 = 50
	// The path is "/tmp/claude_code_mcp_config.json" or similar
	err := os.Remove("/tmp/claude_code_mcp_config.json")
	if err != nil {
		// 0xae02f3-0xae0306: check if os.ErrNotExist
		if !os.IsNotExist(err) {
			// 0xae031d-0xae03ee: slog.Warn
			// Message length 0x33 = 51:
			// "Failed to remove temporary MCP config file"
			e.Logger.WarnContext(ctx,
				"Failed to remove temporary MCP config file",
				"error", err,
			)
		}
	}

	return nil
}

// SetClaudePath sets the command path for the Claude Code binary on the
// executor. It stores the string at offset 0x20 and 0x28 in the struct.
//
// Binary address: 0xae04a0 - 0xae04f5
func (e *ClaudeCodeExecutor) SetClaudePath(path string) {
	// 0xae04af: MOVQ CX, 0x28(AX) — store string len
	// 0xae04cc: MOVQ BX, 0x20(AX) — store string data ptr
	e.ClaudePathCmd = path
}

// printCodeLogs reads the Claude Code log file and prints its contents to
// stdout, framed with separator lines. If the file cannot be read or is
// empty, it logs accordingly and returns.
//
// The function:
//  1. Reads "/tmp/claude_code_logs" (len 0x14 = 20) via os.ReadFile
//  2. If read error: logs a WARN with the error details and returns
//  3. If file is empty: logs an INFO message and returns
//  4. Creates separator line: strings.Repeat("=", 80) (0x50 = 80 repetitions)
//  5. Prints: "\n" + separator, then Claude Code Logs header,
//     then file content (with trailing newline if missing),
//     then separator + "\n"
//
// Binary address: 0xae0500 - 0xae08ee
func (e *ClaudeCodeExecutor) printCodeLogs(ctx context.Context) {
	// 0xae0535: os.ReadFile("/tmp/claude_code_logs") — length 0x14 = 20
	content, err := os.ReadFile("/tmp/claude_code_logs")
	if err != nil {
		// 0xae054f-0xae0613: slog.Warn (level 4)
		// Message length 0x1f = 31: "Failed to read claude code logs"
		e.Logger.WarnContext(ctx,
			"Failed to read claude code logs",
			"error", err,
		)
		return
	}

	if len(content) == 0 {
		// 0xae0621-0xae0696: slog.Info
		// Message length 0x1d = 29: "Claude code logs file empty"
		e.Logger.InfoContext(ctx,
			"Claude code logs file empty",
		)
		return
	}

	// 0xae06bc-0xae06cd: strings.Repeat("=", 80)
	separator := strings.Repeat("=", 80)

	// 0xae06d5-0xae0760: print "\n" + separator to stdout
	header := "\n" + separator
	os.Stdout.Write([]byte(header))

	// 0xae0765-0xae078f: print "Claude Code Logs" header (len 0x28 = 40)
	os.Stdout.Write([]byte("\n===== Claude Code Logs =====\n"))

	// 0xae07bd-0xae07c7: print separator line to stdout
	os.Stdout.Write([]byte(separator))

	// 0xae07cc-0xae07eb: print file content
	os.Stdout.Write(content)

	// 0xae07f8-0xae0808: check if content ends with newline (0x0a)
	if len(content) > 0 && content[len(content)-1] != '\n' {
		// 0xae0824-0xae0833: print newline
		os.Stdout.Write([]byte("\n"))
	}

	// 0xae0838-0xae088e: print separator + "\n"
	footer := separator + "\n"
	os.Stdout.Write([]byte(footer))
}
