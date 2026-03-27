// Reconstructed from a6f96673 DWARF extraction, carried forward to 495ea204.
// Package: internal/session
// Source: internal/session/activity_recorder.go
//
// This file contains the ActivityRecorder interface and SessionActivityRecorder.
// The NoopActivityRecorder and noopStopper are in noop_activity_recorder.go
// (matching the original DWARF source file separation).

package session

import (
	"fmt"
	"log/slog"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/api"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/util"
)

// ActivityRecorder is the interface for recording session activity events.
// Implemented by SessionActivityRecorder (real) and NoopActivityRecorder.
type ActivityRecorder interface {
	RecordActivity(category api.LogCategory, eventType string) error
	RecordFailureResult(category api.LogCategory, eventType string, errMsg string) error
	RecordLongRunningActivity(category api.LogCategory, eventType string) *util.PeriodicInvoker
}

// SessionActivityRecorder posts session activity events to the session ingress API.
//
// Binary struct layout at 0xa94d80
type SessionActivityRecorder struct {
	client    *api.HttpSessionIngressClient // offset 0x00
	logger    *slog.Logger                  // offset 0x10
	sessionID string                        // offset 0x18
}

// NewActivityRecorder creates a new SessionActivityRecorder.
// Binary address: 0xa93ec0
func NewActivityRecorder(client *api.HttpSessionIngressClient, logger *slog.Logger, sessionID string) *SessionActivityRecorder {
	return &SessionActivityRecorder{
		client:    client,
		logger:    logger,
		sessionID: sessionID,
	}
}

// RecordActivity posts a single session event to the session ingress API.
// Binary address: 0xa94120
func (r *SessionActivityRecorder) RecordActivity(category api.LogCategory, eventType string) error {
	err := r.client.PostSessionEvent(r.sessionID, category, eventType)
	if err != nil {
		return fmt.Errorf("failed to post session ingress event: %w", err)
	}
	return nil
}

// RecordFailureResult posts a synthetic assistant event with an error message
// and then posts a result event.
// Binary address: 0xa94280
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

// RecordLongRunningActivity starts a periodic session event poster.
// Binary address: 0xa945a0
func (r *SessionActivityRecorder) RecordLongRunningActivity(category api.LogCategory, eventType string) *util.PeriodicInvoker {
	// Post the initial event
	err := r.client.PostSessionEvent(r.sessionID, category, eventType)
	if err != nil {
		r.logger.Error("Failed to post session ingress event", "error", err)
	}

	// Create a periodic invoker that re-posts the event
	invoker := util.NewPeriodicInvoker(r.logger, 30*time.Second, func() error {
		err := r.client.PostSessionEvent(r.sessionID, category, eventType)
		if err != nil {
			r.logger.Error("Failed to post session ingress event", "error", err)
			return err
		}
		return nil
	})
	invoker.Start()

	return invoker
}
