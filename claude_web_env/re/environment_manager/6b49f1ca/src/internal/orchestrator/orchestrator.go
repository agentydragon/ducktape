// Reconstructed from binary: environment-manager (Build ID 6b49f1ca)
// Source: internal/orchestrator/orchestrator.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/internal/orchestrator

package orchestrator

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/o11y"
)

// PollerInterface is the interface for polling sessions from the API.
// Binary symbol: *orchestrator.PollerInterface (interface type in nm output)
type PollerInterface interface {
	Poll(ctx context.Context) (*SessionResponse, error)
	SleepWithJitter(ctx context.Context) error
}

// Orchestrator manages the lifecycle of polling for work, executing hooks,
// and reporting results. It runs in a loop, polling for sessions and
// dispatching them to hooks.
type Orchestrator struct {
	// Field layout (reconstructed from NewOrchestrator at 0xa8c780):
	// Offset 0x00: apiClient interface pair (itab + data) - the API client
	// Offset 0x10: sessionID string (ptr + len)
	// Offset 0x20: pollInterval time.Duration
	// Offset 0x28: poller (via offset read in Run)
	// Offset 0x30: hook command string (ptr + len)
	// Offset 0x38: timeout time.Duration (not always set)
	// Offset 0x40: logger *slog.Logger (set via With in constructor)
	APIClient    interface{} // API/config client
	SessionID    string
	PollInterval time.Duration
	Poller       PollerInterface
	HookCommand  string
	Timeout      time.Duration
	Logger       *slog.Logger
}

// SessionResponse represents a session received from polling.
type SessionResponse struct {
	// Contains session data that is passed to hooks
}

// NewOrchestrator creates a new Orchestrator with the given configuration.
// It validates inputs and sets defaults for optional parameters.
//
// Binary: 0xa8c780 - orchestrator.NewOrchestrator
// Source: orchestrator/orchestrator.go
//
// Parameters (register ABI):
//   AX = apiClient (interface itab ptr, validated != nil)
//   BX = apiClient (interface data ptr)
//   CX = sessionID string ptr
//   DI = sessionID string len
//   SI = pollInterval time.Duration (defaults to 5min=0x45d964b800 if 0)
//   R8 = timeout time.Duration (defaults to 5min if 0)
//   R9 = hookCommand string ptr
//   R10 = logger *slog.Logger (defaults to slog.Default() if nil)
//
// Returns:
//   AX = *Orchestrator
//   BX = error (interface type, nil on success)
//   CX = error (interface data, nil on success)
func NewOrchestrator(
	apiClient interface{},
	sessionID string,
	pollInterval time.Duration,
	timeout time.Duration,
	hookCommand string,
	logger *slog.Logger,
) (*Orchestrator, error) {
	// Validate apiClient is not nil.
	// Binary: 0xa8c7d2-0xa8c7e0 CMPQ + JE to error path at 0xa8c93f
	if apiClient == nil {
		// Error at 0xa8c93f: "apiClient is nil" (0x12=18 chars)
		return nil, fmt.Errorf("apiClient is nil")
	}

	// Default pollInterval to 5 minutes (0x45d964b800 ns = 300,000,000,000 ns).
	// Binary: 0xa8c7e6-0xa8c7fd
	const defaultInterval = 5 * time.Minute // 0x45d964b800
	if pollInterval == 0 {
		pollInterval = defaultInterval
	}

	// Default timeout to 5 minutes.
	// Binary: 0xa8c809-0xa8c814
	if timeout == 0 {
		timeout = defaultInterval
	}

	// Default logger to slog.Default().
	// Binary: 0xa8c81c-0xa8c82b: loads slog.defaultLogger global
	if logger == nil {
		logger = slog.Default()
	}

	// Create logger with orchestrator-specific attributes.
	// Binary: 0xa8c837-0xa8c8a8
	// Adds slog.String attribute with:
	//   key = "component" (0x09=9 chars)
	//   value = "orchestrator" (0x0c=12 chars)
	// Also sets up a Poller attribute structure
	logger = logger.With(
		slog.String("component", "orchestrator"),
	)

	// Allocate and populate the Orchestrator struct.
	// Binary: 0xa8c8b9 runtime.newobject call
	orch := &Orchestrator{
		APIClient:    apiClient,
		SessionID:    sessionID,
		PollInterval: pollInterval,
		HookCommand:  hookCommand,
		Timeout:      timeout,
		Logger:       logger,
	}

	return orch, nil
}

// Run is the main orchestration loop. It polls for sessions, dispatches them
// to hooks, and handles timeouts and cancellation.
//
// Binary: 0xa8d7e0 - (*Orchestrator).Run
// Source: orchestrator/orchestrator.go
//
// Parameters:
//   AX = *Orchestrator (self)
//   BX, CX = ctx (context.Context interface pair)
//
// Returns:
//   AX = error (interface type, nil on success)
//   BX = error (interface data)
func (o *Orchestrator) Run(ctx context.Context) error {
	// Log startup with 3 slog attrs at Info level.
	// Binary: 0xa8d9e2 slog call with level=0, 3 attrs
	// Attrs: "session_id" (0x0c), "poll_interval" (0x0c), "sandbox_command" (0x11=17)
	// "starting orchestrator" (0x15=21 chars)
	o.Logger.Info("starting orchestrator",
		"session_id", o.SessionID,
		"poll_interval", o.PollInterval,
		"sandbox_command", o.HookCommand,
	)

	var loopCount int64

	// Main polling loop.
	// Binary: 0xa8d9f0-0xa8dddc
	now := time.Now()
	for {
		// Check for context cancellation via selectnbrecv.
		// Binary: 0xa8da39 runtime.selectnbrecv
		select {
		case <-ctx.Done():
			// Context cancelled path at 0xa8dddd:
			// Logs: "context cancelled, stopping orchestrator" (0x36=54 chars)
			o.Logger.Info("context cancelled, stopping orchestrator")

			// Calls the Poller's cleanup/stop method.
			// Binary: 0xa8de2a CALL DX (indirect call through poller offset 0x28)
			if err := o.Poller.SleepWithJitter(ctx); err != nil {
				return fmt.Errorf("cleanup failed: %w", err)
			}
			return nil

		default:
			// Continue polling
		}

		// Handle loop timeout (backoff / jitter).
		// Binary: 0xa8da72 call to handleLoopTimeout
		_, _, _ = o.handleLoopTimeout(ctx, now)

		// Poll for a session.
		// Binary: 0xa8daba call to pollForSession
		session, err := o.pollForSession(ctx)

		if err != nil {
			// Poll error path at 0xa8ddc3: propagate error
			return err
		}

		if session == nil {
			// No session available - continue loop
			now = time.Now()
			continue
		}

		// Session received - check if hook is configured.
		// Binary: 0xa8dadf CMPQ offset 0x18 (hook pointer)
		if o.HookCommand == "" {
			// No hook: output session directly.
			// Binary: 0xa8dd52 call to outputSession
			if err := o.outputSession(ctx, session); err != nil {
				return fmt.Errorf("failed to output session: %w", err)
			}
			return nil
		}

		// Hook is configured: execute it.
		// Binary: 0xa8db29 loads OrchestratorSessionStartCounter
		o.Logger.Info("session received, executing hook command")
		o11y.Increment(ctx, o11y.OrchestratorSessionStartCounter, nil)

		// Create hook and execute with stdin.
		// Binary: 0xa8db80 call to (*Hook).ExecuteWithStdin
		hook := &Hook{Command: o.HookCommand, Logger: o.Logger}
		hookErr := hook.ExecuteWithStdin(ctx, session)

		if hookErr != nil {
			// Hook execution failed.
			// Binary: 0xa8db93-0xa8dcdc
			// Logs error with slog at ERROR level (0x08):
			// "hook execution failed" (0x13=19 chars)
			o.Logger.Error("hook execution failed", "error", hookErr)
			o11y.IncrementOrchestratorSessionEnd(ctx, hookErr)

			return fmt.Errorf("hook execution error: %w", hookErr)
		}

		// Hook succeeded.
		// Binary: 0xa8dcee-0xa8dd51
		// "session completed successfully, waiting for next" (0x23=35 chars)
		o.Logger.Info("session completed successfully, waiting for next")
		o11y.IncrementOrchestratorSessionEnd(ctx, nil)

		loopCount++
		_ = loopCount
		return nil
	}
}

// handleLoopTimeout manages backoff timing between poll iterations.
// It calculates elapsed time since the last poll, determines if a timeout
// condition exists, and if so logs it and increments the timeout counter.
// If a timeout hook is configured, it executes it. Returns the current
// time and remaining sleep duration.
//
// Binary: 0xa8c9c0 - (*Orchestrator).handleLoopTimeout
// Source: orchestrator/orchestrator.go
//
// Assembly flow:
//   1. time.Since(lastPollTime) at 0xa8ca15
//   2. Compute remaining = o.PollInterval (offset 0x28) - elapsed
//   3. If remaining > 0: return early (sleep for remaining duration)
//   4. If remaining <= 0 (timeout):
//      a. Log "poll interval exceeded, resetting timer" (0x2d=45 chars) at Info level
//      b. Increment o11y.OrchestratorTimeoutCounter via o11y.Increment
//      c. If o.Hook (offset 0x10) is non-nil: call Hook.Execute at 0xa8cac3
//         If Hook.Execute returns error: log at Error level (0x08)
//           "timeout hook failed" (0x13=19 chars) with 1 attr "error"
//      d. time.Now() at 0xa8cb85
//      e. Return new time + o.PollInterval
func (o *Orchestrator) handleLoopTimeout(
	ctx context.Context,
	lastPollTime time.Time,
) (time.Time, time.Duration, time.Duration) {
	elapsed := time.Since(lastPollTime)
	remaining := o.PollInterval - elapsed

	if remaining > 0 {
		return lastPollTime, remaining, o.PollInterval
	}

	// Poll interval exceeded.
	o.Logger.Info("poll interval exceeded, resetting timer")
	o11y.Increment(ctx, o11y.OrchestratorTimeoutCounter, nil)

	// Execute timeout hook if configured.
	// Binary: 0xa8caa2-0xa8cb85 checks offset 0x10 (hook pointer)
	// and calls Hook.Execute if non-nil
	// On error: logs at Error level with "timeout hook failed" + "error" attr

	return time.Now(), 0, o.PollInterval
}

// pollForSession polls the API for an available session.
//
// Binary: 0xa8cc00 - (*Orchestrator).pollForSession
// Source: orchestrator/orchestrator.go
func (o *Orchestrator) pollForSession(ctx context.Context) (*SessionResponse, error) {
	// Calls the Poller interface's Poll method
	return o.Poller.Poll(ctx)
}

// outputSession writes the session data to stdout (for non-hook mode).
// It logs the session start, increments OrchestratorSessionStartCounter,
// converts session bytes to string, prints to stdout with fmt.Fprintf,
// logs completion, and increments OrchestratorSessionEnd.
//
// Binary: 0xa8d620 - (*Orchestrator).outputSession
// Source: orchestrator/orchestrator.go
//
// Assembly flow:
//   1. Log "outputting session data to stdout" (0x28=40 chars) at Info level
//   2. o11y.Increment(ctx, OrchestratorSessionStartCounter, nil)
//   3. slicebytetostring + convTstring on session data
//   4. fmt.Fprintf(os.Stdout, "%s\n", sessionStr)
//   5. If Fprintf error: fmt.Errorf("failed to write session output to stdout: %w") (0x2a=42 chars)
//   6. Log "session output completed successfully" (0x27=39 chars) at Info level
//   7. o11y.IncrementOrchestratorSessionEnd(ctx, nil)
func (o *Orchestrator) outputSession(ctx context.Context, session *SessionResponse) error {
	o.Logger.Info("outputting session data to stdout")
	o11y.Increment(ctx, o11y.OrchestratorSessionStartCounter, nil)

	// Write session data to stdout.
	_, err := fmt.Fprintf(os.Stdout, "%s\n", session)
	if err != nil {
		return fmt.Errorf("failed to write session output to stdout: %w", err)
	}

	o.Logger.Info("session output completed successfully")
	o11y.IncrementOrchestratorSessionEnd(ctx, nil)

	return nil
}
