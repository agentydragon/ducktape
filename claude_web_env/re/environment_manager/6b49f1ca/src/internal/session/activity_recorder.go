// Reconstructed from binary at /tmp/em-re/environment-manager
// Build ID: 6b49f1ca, Go 1.25.6
// Package: internal/session
// Source: internal/session/activity_recorder.go

package session

import (
	"fmt"
	"log/slog"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/api"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/util"
)

// ActivityRecorder is the interface for recording session activity events.
// Implemented by SessionActivityRecorder (real) and noopActivityRecorder (cmd package).
//
// Interface methods (from itab analysis):
//   - RecordActivity(category api.LogCategory, eventType string) error
//   - RecordFailureResult(category api.LogCategory, eventType string, errMsg string) error
//   - RecordLongRunningActivity(category api.LogCategory, eventType string) util.Stopper
type ActivityRecorder interface {
	RecordActivity(category api.LogCategory, eventType string) error
	RecordFailureResult(category api.LogCategory, eventType string, errMsg string) error
	RecordLongRunningActivity(category api.LogCategory, eventType string) util.Stopper
}

// SessionActivityRecorder posts session activity events to the session ingress API.
//
// Struct layout (from type equality function at 0xa94d80 and field access patterns):
//   offset 0x00: client api.HttpSessionIngressClient (interface: itab + data pointer)
//   offset 0x10: logger *slog.Logger
//   offset 0x18: sessionID string (ptr + len)
type SessionActivityRecorder struct {
	client    api.HttpSessionIngressClient // offset 0x00
	logger    *slog.Logger                 // offset 0x10
	sessionID string                       // offset 0x18
}

// NewActivityRecorder creates a new SessionActivityRecorder.
//
// Binary address: 0xa93ec0
// Source file: internal/session/activity_recorder.go
func NewActivityRecorder(client api.HttpSessionIngressClient, logger *slog.Logger, sessionID string) *SessionActivityRecorder {
	return &SessionActivityRecorder{
		client:    client,
		logger:    logger,
		sessionID: sessionID,
	}
}

// RecordActivity posts a single session event to the session ingress API.
//
// Binary address: 0xa94120
// Source file: internal/session/activity_recorder.go
func (r *SessionActivityRecorder) RecordActivity(category api.LogCategory, eventType string) error {
	err := r.client.PostSessionEvent(r.sessionID, category, eventType)
	if err != nil {
		return fmt.Errorf("failed to post session ingress event: %w", err)
	}
	return nil
}

// RecordFailureResult posts a synthetic assistant event with an error message
// and then posts a result event.
//
// Binary address: 0xa94280
// Source file: internal/session/activity_recorder.go
func (r *SessionActivityRecorder) RecordFailureResult(category api.LogCategory, eventType string, errMsg string) error {
	err := r.client.PostSyntheticAssistantEvent(r.sessionID, errMsg)
	if err != nil {
		r.logger.Error("Failed to post synthetic assistant event", "error", err)
	}

	err = r.client.PostResultEvent(r.sessionID, category, eventType)
	if err != nil {
		return fmt.Errorf("failed to post result event: %w", err)
	}
	return nil
}

// RecordLongRunningActivity starts a periodic session event poster that
// continues posting events until the returned Stopper is called.
// It posts an initial event immediately, then periodically posts
// "still in progress" log messages.
//
// Binary address: 0xa945a0
// Source file: internal/session/activity_recorder.go
//
// Closures:
//   func1 at 0xa94ae0 - periodic callback that posts the session event
//   func2 at 0xa94780 - logs "(still in progress...)" messages with elapsed minutes
func (r *SessionActivityRecorder) RecordLongRunningActivity(category api.LogCategory, eventType string) util.Stopper {
	// Post the initial event
	err := r.client.PostSessionEvent(r.sessionID, category, eventType)
	if err != nil {
		r.logger.Error("Failed to post session ingress event", "error", err)
	}

	// Create a periodic invoker that re-posts the event and logs progress
	invoker := util.NewPeriodicInvoker(func() {
		// func1 at 0xa94ae0: posts the session event periodically
		err := r.client.PostSessionEvent(r.sessionID, category, eventType)
		if err != nil {
			r.logger.Error("Failed to post session ingress event", "error", err)
		}
	})

	// Start a goroutine that logs "still in progress" messages
	// func2 at 0xa94780: tracks elapsed minutes and logs progress
	go func() {
		minutes := 0
		for invoker.Wait() {
			minutes++
			r.logger.Info(fmt.Sprintf("%s (still in progress...)", eventType),
				"elapsed_minutes", minutes,
			)
		}
		// Final message after the last iteration
		r.logger.Info(fmt.Sprintf(
			"%s (still running after %d minutes.  This is the last message about this...)",
			eventType, minutes,
		))
	}()

	return invoker
}
