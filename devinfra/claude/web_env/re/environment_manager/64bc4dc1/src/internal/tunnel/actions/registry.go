// Reconstructed from environment-manager binary (Build ID: 64bc4dc1)
// Source: internal/tunnel/actions/registry.go
// Module: github.com/anthropics/anthropic/api-go/environment-manager

package actions

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"time"

	isync "sync"

	tunnelpb "github.com/anthropics/anthropic/api-go/environment-manager/internal/tunnel/tunnelpb"
)

// Action defines the interface that all tunnel actions must implement.
type Action interface {
	Name() string
	Timeout() time.Duration
	Execute(ctx context.Context, path string, body []byte, reporter ProgressReporter) (*ActionResult, error)
}

// ResponseSender is the interface for sending responses back through the tunnel.
type ResponseSender interface {
	SendHeaders(resp *tunnelpb.TunnelResponse) error
	SendChunk(resp *tunnelpb.TunnelResponse) error
	SendError(resp *tunnelpb.TunnelResponse) error
}

// ProgressReporter is the interface for reporting action progress.
type ProgressReporter interface {
	SendProgress(step string, message string, percent float64) error
}

// ProgressUpdate represents a progress update message.
type ProgressUpdate struct {
	Type    string  `json:"type"`
	Step    string  `json:"step"`
	Message string  `json:"message"`
	Percent float64 `json:"percent"`
}

// ActionResult holds the result of an action execution.
type ActionResult struct {
	Data interface{}
}

// tunnelProgressReporter implements ProgressReporter by sending progress
// updates through the tunnel as streaming JSON.
//
// Binary address: 0xb3d6c0 (SendProgress)
type tunnelProgressReporter struct {
	client         interface{} // tunnel client for sending responses
	requestID      string
	responseSender ResponseSender
}

// SendProgress sends a progress update through the tunnel as a streaming
// JSON response. Builds a map with "type" ("progress"), "step", "message",
// and "percent" keys, marshals to JSON, appends newline, and sends as
// a non-final (streaming) response.
//
// Binary address: 0xb3d6c0
func (r *tunnelProgressReporter) SendProgress(step string, message string, percent float64) error {
	update := map[string]interface{}{
		"type":    "progress",
		"step":    step,
		"message": message,
		"percent": percent,
	}

	data, err := json.Marshal(update)
	if err != nil {
		return fmt.Errorf("failed to marshal progress: %w", err)
	}

	// Append newline for streaming
	data = append(data, '\n')

	// Create tunnel response with body data and send as streaming chunk
	resp := &tunnelpb.TunnelResponse{
		Body:      data,
		Streaming: true,
	}

	return r.responseSender.SendChunk(resp)
}

// Registry manages a set of named actions and dispatches incoming action
// requests. Uses a mutex for deduplication (prevents the same action+path
// from being executed concurrently).
//
// Binary address: Register at 0xb3b960, Execute at 0xb3bac0
type Registry struct {
	logger  *slog.Logger      // offset 0x00
	actions map[string]Action // offset 0x08
	mu      isync.Mutex       // offset 0x10
	running map[string]bool   // secondary map for deduplication
	client  interface{}       // tunnel client reference
	sender  ResponseSender
}

// NewRegistry creates a new action registry with the given logger.
// Initializes the actions and running maps.
func NewRegistry(logger *slog.Logger) *Registry {
	return &Registry{
		logger:  logger,
		actions: make(map[string]Action),
		running: make(map[string]bool),
	}
}

// Register adds an action to the registry. Stores the action in the actions map
// keyed by its Name(), and logs the registration.
//
// Binary address: 0xb3b960
func (r *Registry) Register(action Action) {
	name := action.Name()
	r.actions[name] = action
	slog.Info("registered action", "action", name)
}

// Execute handles an incoming action request. It:
//  1. Strips the "/__actions/" prefix (11 bytes) from the path
//  2. Splits the remaining path on "/" using stringslite.Cut to get action name and sub-path
//  3. Looks up the action in the registry map
//  4. If not found, returns 404 with JSON error "action %s not found"
//  5. Acquires a mutex and checks a deduplication map
//  6. If the action+path is already running, returns 409 with JSON error "action %s is already running"
//  7. Marks the action as running, sets up defer cleanup (Execute.func1)
//  8. Gets the action's timeout via Timeout() (defaults to 5 min if 0)
//  9. Creates context.WithTimeout
//  10. Launches goroutine (Execute.func2) that creates a tunnelProgressReporter,
//     calls the action's Execute method, and handles the result
//
// Binary address: 0xb3bac0
func (r *Registry) Execute(
	ctx context.Context,
	body []byte,
	headers map[string]string,
	sender ResponseSender,
	rawPath string,
) {
	// Strip "/__actions/" prefix (11 chars)
	path := rawPath
	if len(path) >= 11 && path[:11] == "/__actions/" {
		path = path[11:]
	}

	// Split action name and sub-path using Cut on "/"
	actionName, subPath, found := strings.Cut(path, "/")
	if found {
		// Trim trailing "/" from subPath if present
		if len(subPath) > 0 && subPath[len(subPath)-1] == '/' {
			subPath = subPath[:len(subPath)-1]
		}
	}

	// Look up the action in the registry
	action, ok := r.actions[actionName]
	if !ok {
		// Action not found - return 404
		errMap := make(map[string]interface{})
		errMsg := fmt.Sprintf("action %s not found", actionName)
		errMap["error"] = errMsg
		r.sendJSONResponse(ctx, sender, 404, errMap) // 0x194 = 404
		return
	}

	// Acquire mutex for deduplication check
	r.mu.Lock()

	// Check if action+path combination is already running
	runKey := actionName + "/" + subPath
	if r.running[runKey] {
		// Already running - unlock and return 409
		r.mu.Unlock()

		errMap := make(map[string]interface{})
		errMsg := fmt.Sprintf("action %s is already running", actionName)
		errMap["error"] = errMsg
		r.sendJSONResponse(ctx, sender, 409, errMap) // 0x199 = 409
		return
	}

	// Mark as running
	r.running[runKey] = true
	r.mu.Unlock()

	// Set up defer cleanup to unmark when done
	defer func() {
		r.mu.Lock()
		delete(r.running, runKey)
		r.mu.Unlock()
	}()

	// Get timeout from action (default to 5 minutes if 0)
	timeout := action.Timeout()
	if timeout == 0 {
		timeout = 5 * time.Minute // 0x45d964b800 ns = 300,000,000,000
	}

	// Create context with timeout
	actionCtx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	slog.Info("executing action",
		"action", actionName,
		"path", subPath,
		"final", false,
		"timeout", timeout,
		"sender", sender,
		"body", body,
		"headers", headers,
	)

	// Launch execution in a goroutine
	go func() {
		defer cancel()

		// Create progress reporter
		reporter := &tunnelProgressReporter{
			client:         r.client,
			requestID:      rawPath,
			responseSender: sender,
		}

		// Execute the action
		result, err := action.Execute(actionCtx, subPath, body, reporter)
		if err != nil {
			errMap := make(map[string]interface{})
			errMap["error"] = err.Error()
			r.sendJSONResponse(ctx, sender, 500, errMap)
			return
		}

		// Send success response
		r.sendJSONResponse(ctx, sender, 200, result.Data)
	}()
}

// sendJSONResponse creates an HTTP response with the given status code and
// JSON body. Creates a TunnelResponse with status string, marshals the body
// to JSON, sets Content-Type to "application/json" with charset, sets
// Content-Length, and sends through the tunnel.
//
// Creates two header entries: Content-Type and Content-Length.
//
// Binary address: 0xb3d400
func (r *Registry) sendJSONResponse(
	ctx context.Context,
	sender ResponseSender,
	statusCode int,
	body interface{},
) {
	// Create status string
	status := fmt.Sprintf("%d", statusCode)

	// Marshal the body to JSON
	jsonBody, err := json.Marshal(body)
	if err != nil {
		slog.Error("failed to marshal JSON response", "error", err)
		return
	}

	// Build the tunnel response
	resp := &tunnelpb.TunnelResponse{
		Status:      status,
		Body:        jsonBody,
		ContentType: "application/json; charset=utf-8",
	}

	// Set Content-Length header
	contentLength := fmt.Sprintf("%d", len(jsonBody))
	_ = contentLength

	// Send through the ResponseSender
	sender.SendHeaders(resp)
}
