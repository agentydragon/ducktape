// Reconstructed from binary: environment-manager (Build ID 495ea204)
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
	// Field layout (reconstructed from NewOrchestrator at 0xae19a0):
	// Offset 0x00: poller PollerInterface (interface pair, 16 bytes)
	// Offset 0x10: unknown interface/string pair (16 bytes) — possibly sessionID or config
	// Offset 0x20: pollTimeout time.Duration (defaults to 5min)
	// Offset 0x28: loopTimeout time.Duration (defaults to 5min)
	// Offset 0x30: maxPollFailures int (0 = infinite)
	// Offset 0x38: (original logger from param, overwritten at 0x40)
	// Offset 0x40: logger *slog.Logger (enriched with component=orchestrator)
	//
	// NOTE: The execute-hook command is NOT stored in this struct. When
	// --execute-hook is set, cmd_orchestrator wraps it inside a PollHook
	// (which implements PollerInterface) before passing to NewOrchestrator.
	Poller          PollerInterface
	SessionID       string // field at offset 0x10, exact semantics TBD
	PollInterval    time.Duration
	LoopTimeout     time.Duration
	MaxPollFailures int
	Logger          *slog.Logger
}

// SessionResponse represents a session received from polling.
type SessionResponse struct {
	// Contains session data that is passed to hooks
}

// NewOrchestrator creates a new Orchestrator with the given configuration.
// It validates inputs and sets defaults for optional parameters.
//
// Binary: 0xae19a0 - orchestrator.NewOrchestrator
// Source: orchestrator/orchestrator.go
//
// Parameters (register ABI):
//
//	AX = poller (PollerInterface itab ptr, validated != nil)
//	BX = poller (PollerInterface data ptr)
//	CX = sessionID string ptr (or unknown interface itab)
//	DI = sessionID string len (or unknown interface data)
//	SI = pollTimeout time.Duration (defaults to 5min=0x45d964b800 if 0)
//	R8 = loopTimeout time.Duration (defaults to 5min if 0)
//	R9 = maxPollFailures int (0 = infinite)
//	R10 = logger *slog.Logger (defaults to slog.Default() if nil)
//
// Returns:
//
//	AX = *Orchestrator
//	BX = error (interface type, nil on success)
//	CX = error (interface data, nil on success)
func NewOrchestrator(
	poller PollerInterface,
	sessionID string,
	pollTimeout time.Duration,
	loopTimeout time.Duration,
	maxPollFailures int,
	logger *slog.Logger,
) (*Orchestrator, error) {
	// Validate poller is not nil.
	// Binary: 0xae19f2-0xae1a00 CMPQ + JE to error path at 0xae1b5f
	if poller == nil {
		// Error at 0xae1b5f: "poller is required" (0x12=18 chars)
		return nil, fmt.Errorf("poller is required")
	}

	// Default pollTimeout to 5 minutes (0x45d964b800 ns = 300,000,000,000 ns).
	// Binary: 0xae1a06-0xae1a1d
	const defaultInterval = 5 * time.Minute // 0x45d964b800
	if pollTimeout == 0 {
		pollTimeout = defaultInterval
	}

	// Default loopTimeout to 5 minutes.
	// Binary: 0xae1a29-0xae1a3b
	if loopTimeout == 0 {
		loopTimeout = defaultInterval
	}

	// Default logger to slog.Default().
	// Binary: 0xae1a3c-0xae1a4b: loads slog.defaultLogger global
	if logger == nil {
		logger = slog.Default()
	}

	// Create logger with orchestrator-specific attributes.
	// Binary: 0xae1a52-0xae1acd
	// Adds slog.String attribute with:
	//   key = "component" (0x09=9 chars)
	//   value = "orchestrator" (0x0c=12 chars)
	logger = logger.With(
		slog.String("component", "orchestrator"),
	)

	// Allocate and populate the Orchestrator struct.
	// Binary: 0xae1ad2-0xae1b4c — newobject + 4x movups copies fields from
	// register spill slots, then mov for enriched logger at offset 0x40.
	orch := &Orchestrator{
		Poller:          poller,
		SessionID:       sessionID,
		PollInterval:    pollTimeout,
		LoopTimeout:     loopTimeout,
		MaxPollFailures: maxPollFailures,
		Logger:          logger,
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
//
//	AX = *Orchestrator (self)
//	BX, CX = ctx (context.Context interface pair)
//
// Returns:
//
//	AX = error (interface type, nil on success)
//	BX = error (interface data)
func (o *Orchestrator) Run(ctx context.Context) error {
	// Log startup with 3 slog attrs at Info level.
	// Binary: 0xa8d9e2 slog call with level=0, 3 attrs
	// Attrs: "session_id" (0x0c), "poll_interval" (0x0d=13), "loop_timeout" (0x0c=12)
	// "starting orchestrator" (0x15=21 chars)
	o.Logger.Info("starting orchestrator",
		"session_id", o.SessionID,
		"poll_interval", o.PollInterval,
		"loop_timeout", o.LoopTimeout,
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

		// Session received — dispatch to poller for execution.
		// The Poller handles both polling and execution: a standard Poller
		// outputs the session, while a PollHook (wrapping --execute-hook)
		// pipes it to the hook command. This dispatch is internal to the
		// PollerInterface implementation, not controlled by Orchestrator.
		//
		// Binary: 0xa8db29 loads OrchestratorSessionStartCounter
		o.Logger.Info("session received, dispatching")
		o11y.Increment(ctx, o11y.OrchestratorSessionStartCounter, nil)

		// Binary: 0xa8db80 — indirect call through Poller interface
		// (PollHook.ExecuteWithStdin or direct output depending on poller type)
		if err := o.outputSession(ctx, session); err != nil {
			o.Logger.Error("session execution failed", "error", err)
			o11y.IncrementOrchestratorSessionEnd(ctx, err)
			return fmt.Errorf("session execution error: %w", err)
		}

		// Session succeeded.
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
//  1. time.Since(lastPollTime) at 0xa8ca15
//  2. Compute remaining = o.PollInterval (offset 0x28) - elapsed
//  3. If remaining > 0: return early (sleep for remaining duration)
//  4. If remaining <= 0 (timeout):
//     a. Log "poll interval exceeded, resetting timer" (0x2d=45 chars) at Info level
//     b. Increment o11y.OrchestratorTimeoutCounter via o11y.Increment
//     c. If o.Hook (offset 0x10) is non-nil: call Hook.Execute at 0xa8cac3
//     If Hook.Execute returns error: log at Error level (0x08)
//     "timeout hook failed" (0x13=19 chars) with 1 attr "error"
//     d. time.Now() at 0xa8cb85
//     e. Return new time + o.PollInterval
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
//  1. Log "outputting session data to stdout" (0x28=40 chars) at Info level
//  2. o11y.Increment(ctx, OrchestratorSessionStartCounter, nil)
//  3. slicebytetostring + convTstring on session data
//  4. fmt.Fprintf(os.Stdout, "%s\n", sessionStr)
//  5. If Fprintf error: fmt.Errorf("failed to write session output to stdout: %w") (0x2a=42 chars)
//  6. Log "session output completed successfully" (0x27=39 chars) at Info level
//  7. o11y.IncrementOrchestratorSessionEnd(ctx, nil)
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
