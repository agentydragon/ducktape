// Reconstructed from binary: environment-manager (Build ID 6b49f1ca)
// Source: internal/orchestrator/poll_hook.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/internal/orchestrator

package orchestrator

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"math/rand"
	"os/exec"
	"strings"
	"time"
)

// PollHook combines polling and hook execution for the orchestrator's
// poll-then-execute flow. It wraps a Poller and Hook together.
type PollHook struct {
	// Field layout (reconstructed from Poll at 0xa8e1a0):
	// Offset 0x00: HookCommand string (ptr + len = 16 bytes)
	// Offset 0x10: HookArgs []string (ptr + len + cap = 24 bytes)
	// Offset 0x28: PollTimeout time.Duration (8 bytes)
	// Offset 0x30: StdinContext interface{} (marshaled as JSON for hook stdin)
	// Offset 0x38: Logger *slog.Logger
	HookCommand  string
	HookArgs     []string
	PollTimeout  time.Duration
	StdinContext  interface{} // Marshaled as JSON and piped to hook's stdin
	Logger       *slog.Logger
}

// NewPollHook creates a new PollHook that combines polling with hook execution.
// It sets up logging with component-specific attributes and creates the inner
// Hook struct for command execution.
//
// Binary: 0xa8dec0 - orchestrator.NewPollHook
// Source: orchestrator/poll_hook.go
//
// Parameters (register ABI):
//   AX = poller (interface or concrete reference)
//   BX = hookCommand string ptr
//   CX = hookCommand string len (used for timeout default)
//   DI = hookArgs string ptr
//   SI = hookArgs string len
//   R8 = additional args / sandbox config
//   R9 = workerID or session info
//   R10 = timeout time.Duration
//   R11 = logger *slog.Logger
//
// Returns:
//   AX = *PollHook
func NewPollHook(
	poller interface{},
	hookCommand string,
	hookTimeout time.Duration,
	hookArgs []string,
	sandboxEnabled bool,
	sandboxBackend string,
	sandboxArgs []string,
	logger *slog.Logger,
) *PollHook {
	// Create logger with poll_hook attributes.
	// Binary: 0xa8df22-0xa8dff4
	// slog.(*Logger).With with 2 attrs:
	//   attr1: "component" (0x09=9 chars) = "poll_hook" (0x09=9 chars)
	//   attr2: "command" (0x07=7 chars) = hookCommand
	pollHookLogger := logger.With(
		slog.String("component", "poll_hook"),
		slog.String("command", hookCommand),
	)

	// Default timeout to 5 minutes if not set.
	// Binary: 0xa8e01e-0xa8e02b
	// TESTQ CX, CX; CMOVE DX, CX where DX = 0x6fc23ac00 = 5min in ns
	const defaultTimeout = 5 * time.Minute // 0x6fc23ac00
	if hookTimeout == 0 {
		hookTimeout = defaultTimeout
	}

	// Create the PollHook struct.
	// Binary: 0xa8e005 runtime.newobject
	ph := &PollHook{
		HookCommand: hookCommand,
		HookArgs:    hookArgs,
		PollTimeout: hookTimeout,
		Logger:      pollHookLogger,
	}

	return ph
}

// Poll executes the poll hook command and returns a SessionResponse if work
// is available. It marshals StdinContext as JSON to the hook's stdin, runs
// the command with a timeout, and parses the hook's stdout as JSON. If the
// hook returns empty output or the literal string "null", it indicates no
// work is available and nil is returned.
//
// Binary: 0xa8e1a0 - (*PollHook).Poll
// Source: orchestrator/poll_hook.go
//
// Assembly flow (0xa8e1a0-0xa8eec0, ~3400 bytes):
//  1. Check HookCommand != "" (0xa8e1fa): if empty, return error
//     "poll hook command is not configured"
//  2. Log Debug "executing poll hook" (0xa8e222-0xa8e240)
//  3. context.WithTimeout(ctx, ph.PollTimeout) (0xa8e261)
//     defer cancel()
//  4. os/exec.CommandContext(ctx, ph.HookCommand, ph.HookArgs...) (0xa8e28e)
//  5. json.Marshal(ph.StdinContext) (0xa8e2ae)
//     If error: return fmt.Errorf("failed to serialize poll hook stdin context: %w", err)
//  6. Set cmd.Stdin = bytes.NewReader(jsonBytes) (0xa8e3cf-0xa8e3fb)
//  7. Set cmd.Stdout = &bytes.Buffer{} (stderr buffer also allocated)
//     Set cmd.Stderr = &bytes.Buffer{}
//  8. time.Now(); cmd.Run(); time.Since(startTime) (0xa8e480-0xa8e4d7)
//  9. If cmd.Run error (0xa8e4e7):
//     - Read stderr buffer as string
//     - slog.Warn "poll hook execution failed" with 3 attrs:
//       "duration", "stderr", "error"
//     - Read stdout buffer as string
//     - return nil, fmt.Errorf("poll hook failed after %s: %w (stderr: %s)",
//         elapsed, err, stderrStr)
// 10. If cmd.Run success (0xa8e88d):
//     - Read stdout buffer, strings.TrimSpace
//     - If output == "" || output == "null" (0xa8e900-0xa8e910):
//       slog.Debug "poll hook returned empty queue" with 1 attr: "duration"
//       return nil, nil
//     - Else: json.Unmarshal(output, &SessionResponse{}) (0xa8ea80)
//       If unmarshal error:
//         slog.Warn "poll hook returned invalid JSON" with 3 attrs:
//           "duration", "output", "error"
//         return nil, fmt.Errorf("poll hook returned invalid JSON: %w", err)
//       If success:
//         slog.Info "poll hook returned work" with 1 attr: "duration"
//         return &response, nil
func (ph *PollHook) Poll(ctx context.Context) (*SessionResponse, error) {
	// Check if hook command is configured
	// Binary: 0xa8e1fa-0xa8e200
	if ph.HookCommand == "" {
		return nil, fmt.Errorf("poll hook command is not configured")
	}

	// Log at Debug level
	// Binary: 0xa8e21e-0xa8e240
	// DI = -4 (Debug), "executing poll hook" (0x13=19 chars), 0 attrs
	ph.Logger.Debug("executing poll hook")

	// Create timeout context
	// Binary: 0xa8e24d-0xa8e266
	ctx, cancel := context.WithTimeout(ctx, ph.PollTimeout)
	defer cancel()

	// Build command
	// Binary: 0xa8e27b-0xa8e28e
	cmd := exec.CommandContext(ctx, ph.HookCommand, ph.HookArgs...)

	// Marshal StdinContext as JSON for hook's stdin
	// Binary: 0xa8e2a3-0xa8e2ae
	stdinJSON, err := json.Marshal(ph.StdinContext)
	if err != nil {
		// Binary: 0xa8e2b3-0xa8e2fc
		// fmt.Errorf("failed to serialize poll hook stdin context: %w", err)
		return nil, fmt.Errorf("failed to serialize poll hook stdin context: %w", err)
	}

	// Set up stdin from JSON bytes
	// Binary: 0xa8e365-0xa8e3fb
	cmd.Stdin = bytes.NewReader(stdinJSON)

	// Set up stdout and stderr buffers
	// Binary: 0xa8e3ff-0xa8e479
	var stdoutBuf, stderrBuf bytes.Buffer
	cmd.Stdout = &stdoutBuf
	cmd.Stderr = &stderrBuf

	// Run command and measure elapsed time
	// Binary: 0xa8e480-0xa8e4d7
	startTime := time.Now()
	err = cmd.Run()
	elapsed := time.Since(startTime)

	if err != nil {
		// Command execution failed
		// Binary: 0xa8e4ed-0xa8e88c

		// Read stderr output (or "<nil>" if buffer empty)
		// Binary: 0xa8e52e-0xa8e591 (slicebytetostring from stderrBuf)
		stderrStr := stderrBuf.String()

		// Log warning with 3 attrs: duration, stderr, error
		// Binary: 0xa8e5a0-0xa8e70b
		// DI = 8 (Warn), "poll hook execution failed" (0x1a=26 chars), 3 attrs
		ph.Logger.Warn("poll hook execution failed",
			slog.Duration("duration", elapsed),
			slog.String("stderr", stderrStr),
			slog.Any("error", err),
		)

		// Read stdout for the error message
		// Binary: 0xa8e710-0xa8e753 (slicebytetostring from stdoutBuf)
		stdoutStr := stdoutBuf.String()

		// Return formatted error
		// Binary: 0xa8e789-0xa8e820
		// "poll hook failed after %s: %w (stderr: %s)" (0x2a=42 chars)
		return nil, fmt.Errorf("poll hook failed after %s: %w (stderr: %s)",
			elapsed, err, stdoutStr)
	}

	// Command succeeded - read and process stdout
	// Binary: 0xa8e88d-0xa8e8e1
	output := strings.TrimSpace(stdoutBuf.String())

	// Check if output is empty or "null" (no work available)
	// Binary: 0xa8e8e9-0xa8e910
	// CMPQ BX, $4; CMPL 0(AX), $0x6c6c756e ("null" in little-endian)
	if output == "" || output == "null" {
		// Log "poll hook returned empty queue" at Debug level
		// Binary: 0xa8e916-0xa8e9e1
		// DI = -4 (Debug), 0x1e=30 chars, 1 attr: "duration"
		ph.Logger.Debug("poll hook returned empty queue",
			slog.Duration("duration", elapsed),
		)
		return nil, nil
	}

	// Output is non-empty and not "null" - parse as SessionResponse
	// Binary: 0xa8ea47-0xa8ea80
	var response SessionResponse
	if err := json.Unmarshal([]byte(output), &response); err != nil {
		// Unmarshal failed
		// Binary: 0xa8ea88-0xa8ec51
		// slog.Warn "poll hook returned invalid JSON" (0x1f=31 chars)
		// 3 attrs: "duration", "output", "error"
		ph.Logger.Warn("poll hook returned invalid JSON",
			slog.Duration("duration", elapsed),
			slog.String("output", output),
			slog.Any("error", err),
		)
		// Binary: 0xa8ec56-0xa8ecab
		// "poll hook returned invalid JSON: %w" (0x23=35 chars)
		return nil, fmt.Errorf("poll hook returned invalid JSON: %w", err)
	}

	// Successful parse - log and return the response
	// Binary: 0xa8ed14-0xa8edda
	// DI = 0 (Info), "poll hook returned work" (0x17=23 chars), 1 attr: "duration"
	ph.Logger.Info("poll hook returned work",
		slog.Duration("duration", elapsed),
	)

	// Binary: 0xa8eddf-0xa8ee55
	// Return session response fields from the unmarshaled struct
	return &response, nil
}

// SleepWithJitter sleeps for a random duration between 1 and 3 seconds,
// returning early with an error if the context is cancelled.
//
// Binary: 0xa8ef40 - (*PollHook).SleepWithJitter
// Source: orchestrator/poll_hook.go
//
// Assembly flow (0xa8ef40-0xa8f12f):
//  1. Compute jitter: rand.Intn(3) + 1 -> multiply by 0x3b9aca00 (1s in ns)
//     Result: random duration of 1, 2, or 3 seconds
//  2. Log at Debug level: "sleeping with jitter after empty queue"
//     with slog.Duration("sleep_duration", duration) attr
//  3. Create time.NewTimer(duration)
//  4. Call ctx.Done() to get the done channel
//  5. select {
//       case <-ctx.Done(): return fmt.Errorf("jitter sleep interrupted: %w", ctx.Err())
//       case <-timer.C:    return nil
//     }
func (ph *PollHook) SleepWithJitter(ctx context.Context) error {
	// Compute random jitter: 1-3 seconds
	// Binary: 0xa8ef72-0xa8ef87
	// MOVL $0x3, AX; CALL math/rand.Intn; LEAQ 0x1(AX), CX;
	// IMULQ $0x3b9aca00, CX, CX
	jitter := time.Duration(rand.Intn(3)+1) * time.Second

	// Log the sleep duration at Debug level
	// Binary: 0xa8ef8c-0xa8f043
	// DI = -4 (slog.LevelDebug)
	// SI = "sleeping with jitter after empty queue" (0x26=38 chars)
	// 1 attr: slog.Duration("sleep_duration", jitter)
	ph.Logger.Debug("sleeping with jitter after empty queue",
		slog.Duration("sleep_duration", jitter),
	)

	// Create timer and select on timer.C vs ctx.Done()
	// Binary: 0xa8f04d-0xa8f0b3
	timer := time.NewTimer(jitter)
	select {
	case <-ctx.Done():
		// Context cancelled: return wrapped error
		// Binary: 0xa8f0bd-0xa8f122
		// Calls ctx.Err(), wraps with fmt.Errorf("jitter sleep interrupted: %w", ...)
		return fmt.Errorf("jitter sleep interrupted: %w", ctx.Err())
	case <-timer.C:
		// Timer fired: sleep completed successfully
		// Binary: 0xa8f123-0xa8f12f
		// Returns nil, nil
		return nil
	}
}
