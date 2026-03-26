// Reconstructed from binary: environment-manager (Build ID 64bc4dc1)
// Source: internal/orchestrator/hooks.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/internal/orchestrator

package orchestrator

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os/exec"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/sandbox"
)

// Hook represents a command that is executed when a session is received.
// It wraps command execution with timeout, sandbox support, and logging.
type Hook struct {
	// Field layout (reconstructed from Execute/ExecuteWithStdin disassembly):
	// Offset 0x00: name string (ptr + len)
	// Offset 0x10: command string (ptr + len)
	// Offset 0x18: args []string (ptr + len + cap)
	// Offset 0x30: ...
	// Offset 0x38: timeout time.Duration
	// Offset 0x40: useSandbox bool
	// Offset 0x48-0x60: sandbox config (paths, etc.)
	Name          string
	Command       string
	Args          []string
	Timeout       time.Duration
	UseSandbox    bool
	SandboxConfig *sandbox.SandboxConfig
	Logger        *slog.Logger
}

// SessionInput is the JSON payload passed to hooks via stdin.
type SessionInput struct {
	SessionID string `json:"session_id,omitempty"`
	WorkID    string `json:"work_id,omitempty"`
	// Additional session fields
}

// Execute runs the hook command with the given context and returns any error.
// It supports optional timeouts and logs command execution details.
//
// Binary: 0xa8af60 - (*Hook).Execute
// Source: orchestrator/hooks.go
//
// Parameters:
//
//	AX = *Hook (self)
//	BX, CX = ctx (context.Context interface pair)
//	DI = logger *slog.Logger
//
// Returns:
//
//	AX = error (interface type, nil on success)
//	BX = error (interface data)
func (h *Hook) Execute(ctx context.Context) error {
	// Check if hook command is set (offset 0x18 != 0).
	// Binary: 0xa8afa5 CMPQ 0x18(AX), $0x0; JE 0xa8b658 (nil return)
	if h.Command == "" {
		return nil
	}

	// Create logger with hook-specific attributes.
	// Binary: 0xa8b065-0xa8b0c8 slog.(*Logger).With with 2 attrs:
	//   attr1: "hook" (0x04=4 chars) struct { name (0x04), len, command_ptr, name_ptr, command (0x07=7 chars) }
	//   attr2: "command" (0x07=7 chars) struct { ... }
	hookLogger := h.Logger.With(
		slog.String("hook", h.Name),
		slog.String("command", h.Command),
	)

	// Log "executing hook" (0x0e=14 chars)
	// Binary: 0xa8b100 slog.(*Logger).log
	hookLogger.Info("executing hook")

	// Apply timeout if configured.
	// Binary: 0xa8b10d-0xa8b162
	// Checks h.Timeout (offset 0x38) > 0; if so, calls context.WithTimeout
	var cancel context.CancelFunc
	hasTimeout := h.Timeout > 0
	if hasTimeout {
		ctx, cancel = context.WithTimeout(ctx, h.Timeout)
		defer cancel()
	}

	// Build the command with CommandContext.
	// Binary: 0xa8b180 os/exec.CommandContext
	// Uses h.Command (offset 0x10) and h.Args (offset 0x18-0x30)
	cmd := exec.CommandContext(ctx, h.Command, h.Args...)

	// Execute and capture combined output.
	// Binary: 0xa8b1ac os/exec.(*Cmd).CombinedOutput
	startTime := time.Now()
	output, err := cmd.CombinedOutput()
	elapsed := time.Since(startTime)

	if err != nil {
		// Error path at 0xa8b1ed:
		// Logs at ERROR level (0x08) with 3 slog attrs:
		//   "duration" (0x08=8 chars) + duration value
		//   "output" (0x06=6 chars) + string(output)
		//   "error" (0x05=5 chars) + err
		// Message: "hook execution failed" (0x15=21 chars)
		hookLogger.Error("hook execution failed",
			"duration", elapsed,
			"output", string(output),
			"error", err,
		)

		// Creates formatted error with hook name, duration, error, and output.
		// Binary: 0xa8b4a0 fmt.Errorf
		// "hook %q failed after %v: %w (output: %s)" (0x26=38 chars)
		return fmt.Errorf("hook %q failed after %v: %w (output: %s)",
			h.Name, elapsed, err, string(output))
	}

	// Success path at 0xa8b4ea:
	// Logs at Info level (0x00) with 2 slog attrs:
	//   "duration" (0x08=8 chars)
	//   "output" (0x06=6 chars)
	// Message: "hook executed successfully" (0x18=24 chars)
	hookLogger.Info("hook executed successfully",
		"duration", elapsed,
		"output", string(output),
	)

	return nil
}

// ExecuteWithStdin runs the hook command, passing the session data as JSON
// via stdin. It supports sandbox wrapping and optional timeouts.
//
// Binary: 0xa8b6c0 - (*Hook).ExecuteWithStdin
// Source: orchestrator/hooks.go
//
// Parameters:
//
//	AX = *Hook (self)
//	BX, CX = ctx (context.Context interface pair)
//	DI = logger *slog.Logger
//	SI = session data ([]byte ptr)
//	R8 = session data ([]byte len)
//	R9 = session data ([]byte cap)
//
// Returns:
//
//	AX = error (interface type, nil on success)
//	BX = error (interface data)
func (h *Hook) ExecuteWithStdin(ctx context.Context, session *SessionResponse) error {
	// Check if hook command is set.
	// Binary: 0xa8b70c CMPQ 0x18(AX), $0x0; JE 0xa8c5fb
	if h.Command == "" {
		return nil
	}

	// Create logger with 3 hook-specific attributes.
	// Binary: 0xa8b82b-0xa8b8c0 slog.(*Logger).With with 3 attrs:
	//   "hook" (0x04), "command" (0x07), "stdin_enabled" (0x0f=15 chars)
	hookLogger := h.Logger.With(
		slog.String("hook", h.Name),
		slog.String("command", h.Command),
		slog.Bool("stdin_enabled", h.UseSandbox),
	)

	// Log "executing hook with stdin" (0x19=25 chars)
	// Binary: 0xa8b8e0-0xa8b8f6 slog.(*Logger).log
	hookLogger.Info("executing hook with stdin")

	// Apply timeout if configured.
	// Binary: 0xa8b903-0xa8b94b
	var cancel context.CancelFunc
	hasTimeout := h.Timeout > 0
	if hasTimeout {
		ctx, cancel = context.WithTimeout(ctx, h.Timeout)
		defer cancel()
	}

	// Unmarshal session input for enrichment.
	// Binary: 0xa8b960-0xa8b994 runtime.newobject + json.Unmarshal
	var sessionInput SessionInput
	sessionBytes, _ := json.Marshal(session)
	if err := json.Unmarshal(sessionBytes, &sessionInput); err != nil {
		// Log warning but continue.
		// Binary: 0xa8ba48 slog call at WARN level (0x04)
		// "failed to unmarshal session input" (0x25=37 chars)
		hookLogger.Warn("failed to unmarshal session input", "error", err)
	}

	// Check if session has a work_id for the --stdin arg.
	// Binary: 0xa8ba5a-0xa8bb25
	if sessionInput.WorkID != "" {
		// Log "using session work_id from stdin" (0x1e=30 chars)
		hookLogger.Info("using session work_id from stdin",
			"work_id", sessionInput.WorkID,
		)
	}

	// Build the command arguments, potentially adding --stdin args.
	// Binary: 0xa8bb25-0xa8bc22
	// Appends "--stdin" (0x09=9 chars) and potentially "--input-format=v1"
	args := make([]string, len(h.Args))
	copy(args, h.Args)
	if sessionInput.WorkID != "" {
		args = append(args, "--stdin", "--input-format=v1")
	}

	// Check if sandbox wrapping is needed.
	// Binary: 0xa8bc33-0xa8bc39 CMPB 0x40(R12), $0x0
	if h.UseSandbox && h.SandboxConfig != nil {
		// Create sandbox runtime.
		// Binary: 0xa8bc8f call to sandbox.NewSandboxRuntimeWithConfig
		sbRuntime, err := sandbox.NewSandboxRuntimeWithConfig(h.SandboxConfig, hookLogger)
		if err != nil {
			// Error path at 0xa8bc9d:
			// Logs error and returns.
			// "failed to create sandbox runtime" (0x1d=29 chars)
			hookLogger.Error("failed to create sandbox runtime", "error", err)
			return fmt.Errorf("failed to create sandbox runtime: %w", err)
		}

		// Wrap command with sandbox.
		// Binary: 0xa8be99 call to sandbox.(*SandboxRuntime).WrapCommand
		wrappedCmd, wrappedArgs := sbRuntime.WrapCommand(h.Command, args)

		// Log wrapped command.
		// Binary: 0xa8bfcb-0xa8bfe9 slog log
		// "executing sandboxed command" (0x1c=28 chars)
		hookLogger.Info("executing sandboxed command",
			"sandbox_command", wrappedCmd,
			"sandbox_args", wrappedArgs,
		)

		h.Command = wrappedCmd
		args = wrappedArgs
	}

	// Build and execute the command with CommandContext.
	// Binary: 0xa8c038 os/exec.CommandContext
	cmd := exec.CommandContext(ctx, h.Command, args...)

	// Set up stdin pipe with session data.
	// Binary: 0xa8c04c-0xa8c065
	// Creates a bytes.Reader for the session JSON and assigns to cmd.Stdin
	cmd.Stdin = bytes.NewReader(sessionBytes)

	// Set up a closure for deferred cleanup.
	// Binary: 0xa8be1d-0xa8be6d func1 closure at 0xa8c680
	// The closure captures: cmd, hookLogger, ctx for cleanup on completion

	// Execute and capture combined output.
	startTime := time.Now()
	output, err := cmd.CombinedOutput()
	elapsed := time.Since(startTime)

	if err != nil {
		// Error path: similar to Execute error handling
		hookLogger.Error("hook with stdin execution failed",
			"duration", elapsed,
			"output", string(output),
			"error", err,
		)
		return fmt.Errorf("hook %q failed after %v: %w (output: %s)",
			h.Name, elapsed, err, string(output))
	}

	hookLogger.Info("hook with stdin executed successfully",
		"duration", elapsed,
		"output", string(output),
	)

	return nil
}

// ExecuteWithStdin.func1 is a closure used for deferred cleanup during
// hook execution with stdin.
//
// Binary: 0xa8c680 - (*Hook).ExecuteWithStdin.func1
// Source: orchestrator/hooks.go
