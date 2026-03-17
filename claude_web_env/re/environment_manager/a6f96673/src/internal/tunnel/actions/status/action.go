// Reconstructed from binary: environment-manager (Build ID a6f96673)
// Source: internal/tunnel/actions/status/action.go
// Module: github.com/anthropics/anthropic/api-go/environment-manager
//
// Package status implements the status tunnel action.

package status

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/tunnel/actions"
)

// statusResult holds the JSON response for the status action.
//
// Binary type: *status.statusResult
type statusResult struct {
	Port    int64  `json:"port"`
	Status  string `json:"status"`
	Message string `json:"message"`
}

// StatusAction implements the actions.Action interface for status checks.
// It parses a port from the request body and returns a status response.
//
// Binary type: *status.StatusAction
type StatusAction struct {
	Logger *slog.Logger
}

// NewStatusAction creates a new StatusAction.
func NewStatusAction(logger *slog.Logger) *StatusAction {
	return &StatusAction{
		Logger: logger,
	}
}

// Name returns "status" (6 chars).
//
// Binary: 0xba3a20 - (*StatusAction).Name
func (a *StatusAction) Name() string {
	return "status"
}

// Timeout returns 5 seconds.
//
// Binary: 0xba3a40 - (*StatusAction).Timeout
// 0x12a05f200 = 5,000,000,000 ns = 5s
func (a *StatusAction) Timeout() time.Duration {
	return 5 * time.Second
}

// Execute performs the status check action.
//
// Binary: 0xba3a60 - (*StatusAction).Execute
// Source: action.go:44
//
// Flow:
//  1. Allocate statusResult (action.go:49)
//  2. Unmarshal body into statusResult (action.go:50-51)
//  3. If unmarshal error, return fmt.Errorf "failed to parse body: %w" (action.go:52)
//  4. If port is 0, set default port 3000 (0xbb8) (action.go:55-56)
//  5. Validate port is <= 65534 (0xfffe) (action.go:58)
//  6. If invalid, return error (action.go:62)
//  7. Build status string from port (action.go:62)
//  8. Build result message (action.go:63-64)
//  9. Return ActionResult
func (a *StatusAction) Execute(ctx context.Context, path string, body []byte, reporter actions.ProgressReporter) (*actions.ActionResult, error) {
	// action.go:49 - Parse request body
	var result statusResult
	if len(body) > 0 {
		// action.go:50-51 - Unmarshal JSON body
		if err := json.Unmarshal(body, &result); err != nil {
			// action.go:52 - Parse error
			return nil, fmt.Errorf("failed to parse body: %w", err)
		}
	}

	// action.go:55-56 - Default port to 3000 if not set
	if result.Port == 0 {
		result.Port = 3000
	}

	// action.go:58 - Validate port range
	if result.Port-1 > 65534 {
		// action.go:62 - Invalid port
		return nil, fmt.Errorf("invalid port: %d", result.Port)
	}

	// action.go:62 - Build status message
	result.Status = fmt.Sprintf("%d", result.Port)

	// action.go:63-64 - Set message with "ok" prefix and details
	result.Message = "ok"

	return &actions.ActionResult{Data: &result}, nil
}
