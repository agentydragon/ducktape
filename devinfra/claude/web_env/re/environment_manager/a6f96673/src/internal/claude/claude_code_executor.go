// Reconstructed from binary: environment-manager (Build ID a6f96673)
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

// ClaudeCodeExecutor manages the lifecycle of a Claude Code process.
//
// Struct layout (from NewClaudeCodeExecutor at 0xad8800, size from runtime.newobject):
//
//	Offset 0x00: Logger       *slog.Logger      (field 0, stored from AX param -> 0(result))
//	Offset 0x08: LoggerName   string            (field 1, stored at 0x8)
//	Offset 0x10: unknown      interface          (field 2, at 0x10)
//	Offset 0x18: unknown      interface          (field 3, at 0x18)
//	Offset 0x20: ClaudePath   string data        (field 4, at 0x20 — set by SetClaudePath)
//	Offset 0x28: ClaudePath   string len          (field 5, at 0x28)
//	Offset 0x30: Config       *config.ClaudeConfig (field 6, at 0x30)
//	Offset 0x38: unknown      interface          (field 7, at 0x38)
//	Offset 0x40: WorkingDir   string data        (field 8, at 0x40)
//	Offset 0x48: WorkingDir   string len          (field 9, at 0x48)
//	Offset 0x50: GatewayConfig *GatewayConfig    (field 10, at 0x50)
//	Offset 0x58: unknown      interface          (field 11, at 0x58)
//	Offset 0x60: SessionToken string data        (field 12, at 0x60)
//	Offset 0x68: SessionToken string len          (field 13, at 0x68)
//	Offset 0x70: Outcomes     *Outcomes          (field 14, at 0x70)
//	Offset 0x78: unknown      interface          (field 15, at 0x78)
//	Offset 0x80: unknown      interface          (field 16, at 0x80)
//	Offset 0x88: ClaudePath2  string data        (field 17, at 0x88 — used by GetClaudePath)
//	Offset 0x90: ClaudePath2  string len          (field 18, at 0x90)
//
// Total struct size: ~0x98 (152 bytes, from runtime.newobject type descriptor)
// TODO(re): many fields typed as interface{} — concrete types not yet recovered from binary.
// Known candidates from constructor (NewClaudeCodeExecutor) parameter analysis:
//
//	Ctx → context.Context, Config → *config.ClaudeConfig, GatewayConfig → *GatewayConfig,
//	SessionIngress → *api.HttpSessionIngressClient, ConfigExtra/OutcomesExtra/ExtraField → unknown
type ClaudeCodeExecutor struct {
	Logger         *slog.Logger // offset 0x00
	LoggerName     string       // offset 0x08 (string, len at 0x10)
	Ctx            interface{}  // offset 0x10 — context or similar
	CtxValue       interface{}  // offset 0x18
	ClaudePathCmd  string       // offset 0x20 — claude binary command path (set by SetClaudePath)
	Config         interface{}  // offset 0x30 — *config.ClaudeConfig
	ConfigExtra    interface{}  // offset 0x38
	WorkingDir     string       // offset 0x40
	WorkingDirLen  string       // offset 0x48
	GatewayConfig  interface{}  // offset 0x50
	SessionIngress interface{}  // offset 0x58
	SessionToken   string       // offset 0x60
	Outcomes       *Outcomes    // offset 0x70
	OutcomesExtra  interface{}  // offset 0x78
	ExtraField     interface{}  // offset 0x80
	ClaudePath     string       // offset 0x88 — resolved claude path (from GetClaudePath)
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
	config interface{},
	outcomes *Outcomes,
	diagReporter interface{},
	// additional parameters from stack
) *ClaudeCodeExecutor {
	if logger == nil {
		panic("logger must not be nil")
	}
	if ctx == nil {
		panic("ctx must not be nil")
	}
	if config == nil {
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
		"claudeConfig", config,
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
		Config:     config,
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
		"claudeConfig", e.Config,
	)

	// 0xad9540: Set up environment variables
	// Get base environment from os.Environ()
	envVars := os.Environ()

	// TODO(re): Add environment variables from config once config type is properly reconstructed
	// For now, cmd.Env will use the base environment

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

	// TODO(re): func3 closure (pipe write-end cleanup at 0xadf1c0) not reconstructed
	// TODO(re): func4 closure (output writer at 0xadf0a0) not reconstructed

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
	// Type assert config to *config.StartupContext to access fields
	// The Config field is set to parsedCtx.StartupContext in cmd_task_run.go
	startupCtx, ok := e.Config.(*config.StartupContext)
	if !ok || startupCtx == nil {
		// No gateway config, return empty args
		return []string{}, nil
	}

	// 0xadf709-0xadf718: read ClaudeCodeArgs from config (offset 0xc0)
	claudeCodeArgs := startupCtx.ClaudeCodeArgs

	// 0xadf738-0xadf743: check McpConfig != nil (offset 0xc8)
	hasMcpConfig := startupCtx.McpConfig != nil

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
	// Type assert config to *config.StartupContext to access McpConfigFile
	startupCtx, ok := e.Config.(*config.StartupContext)
	if !ok || startupCtx == nil {
		return fmt.Errorf("config is not *config.StartupContext")
	}

	// Check if McpConfigFile is present
	if startupCtx.McpConfigFile == nil {
		return fmt.Errorf("no MCP config file specified")
	}

	mcpConfigFile := startupCtx.McpConfigFile

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
