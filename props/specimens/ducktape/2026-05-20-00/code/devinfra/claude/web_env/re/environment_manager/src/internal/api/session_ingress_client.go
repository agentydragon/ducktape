// Reconstructed from binary: Build ID 495ea204
// Source: internal/api/session_ingress_client.go
//
// Original source path:
//   /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/api/session_ingress_client.go

package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"

	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/propagation"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/util"
)

// ErrEndpointNotImplemented is a sentinel error for 501 Not Implemented responses.
//
// Binary address: 0x158a9f0
var ErrEndpointNotImplemented = errors.New("endpoint not implemented")

// HttpSessionIngressClient is the client for posting session ingress events
// and diagnostic logs to the Anthropic API.
//
// Binary type eq: 0x832f20
//
// Struct layout (verified via type:.eq at 0x832f20):
//
//	0x00:      *HttpClient   (pointer, compared directly)
//	0x08-0x17: ApiKey string (ptr + len, compared via memequal on ptr, len compared)
//	0x18:      Logger *slog.Logger (pointer, compared directly)
//	0x20:      UseV2  bool   (byte, compared directly)
type HttpSessionIngressClient struct {
	Client *HttpClient  // offset 0x00
	ApiKey string       // offset 0x08 (string: ptr at 0x08, len at 0x10)
	Logger *slog.Logger // offset 0x18
	UseV2  bool         // offset 0x20
}

// sessionEndpoint builds the full URL for a session ingress endpoint.
//
// Format: "%s/%s/session_ingress/session/%s/%s" (35 = 0x23 bytes)
// Arguments: baseURL, version ("v1" or "v2" based on UseV2 flag), url-escaped sessionID, action
//
// Binary address: 0x82fcc0
func (c *HttpSessionIngressClient) sessionEndpoint(sessionID string, action string) string {
	escapedSessionID := url.PathEscape(sessionID)

	version := "v1"
	if c.UseV2 {
		version = "v2"
	}

	return fmt.Sprintf("%s/%s/session_ingress/session/%s/%s",
		c.Client.BaseURL,
		version,
		escapedSessionID,
		action,
	)
}

// postJSON marshals a payload to JSON and POSTs it to the given endpoint.
//
// Sets headers:
//   - Authorization: Bearer <apiKey> (via setAuthHeader)
//   - Content-Type: application/json (12 + 16 bytes)
//   - X-Environment-Manager-Version: <version> (28 = 0x1c bytes header name)
//
// Injects OpenTelemetry trace context via propagation.HeaderCarrier.
//
// Uses RetryableHTTPDo for the actual HTTP call.
// On success (200): returns nil error.
// On 501 (Not Implemented): returns wrapped ErrEndpointNotImplemented with details.
// On other non-200: returns error with endpoint, status code, and response body.
// On HTTP error: returns error with endpoint and underlying error.
// On JSON marshal error: returns error with endpoint and marshal error.
// On request creation error: returns error with endpoint and creation error.
//
// Binary address: 0x82fe40
func (c *HttpSessionIngressClient) postJSON(
	ctx context.Context,
	endpoint string,
	payload interface{},
	contentType string,
) error {
	jsonData, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("failed to marshal JSON for %s: %w", endpoint, err)
	}

	body := bytes.NewReader(jsonData)
	req, err := http.NewRequestWithContext(ctx, "POST", endpoint, body)
	if err != nil {
		return fmt.Errorf("failed to create request for %s: %w", endpoint, err)
	}

	// Inject OpenTelemetry propagation context.
	// Uses globalPropagators from go.opentelemetry.io/otel/internal/global.
	otel.GetTextMapPropagator().Inject(ctx, propagation.HeaderCarrier(req.Header))

	// Set auth header.
	c.Client.setAuthHeader(req, c.ApiKey)

	// Set Content-Type header.
	req.Header.Set("Content-Type", "application/json")

	// Set X-Environment-Manager-Version header from util.Version.
	req.Header.Set("X-Environment-Manager-Version", util.Version)

	// Execute with retries.
	resp, err := c.Client.RetryableHTTPDo(ctx, req, nil)
	if err != nil {
		return fmt.Errorf("request to %s failed: %w", endpoint, err)
	}
	defer resp.Body.Close()

	if resp.StatusCode == http.StatusOK {
		return nil
	}

	// Read response body for error reporting.
	respBody, err := io.ReadAll(resp.Body)
	if err != nil {
		// Ignoring read error, just report status.
	}

	if resp.StatusCode == http.StatusNotImplemented { // 501
		return fmt.Errorf(
			"endpoint %s returned 501: status=%d, body=%s: %w",
			endpoint, resp.StatusCode, string(respBody), ErrEndpointNotImplemented,
		)
	}

	return fmt.Errorf(
		"unexpected response from %s: status=%d, body=%s",
		endpoint, resp.StatusCode, string(respBody),
	)
}

// PostSessionIngressEvent posts a session ingress event to the session's "events" endpoint.
//
// Builds the endpoint URL via sessionEndpoint with action "events" (6 bytes).
// Creates a map with key "events" containing the event data slice.
// Content-Type: "session_event" (13 = 0x0d bytes).
//
// Logs at Debug level before posting, and at Warn level if the post returns nil (success fallthrough).
//
// Binary address: 0x8309a0
func (c *HttpSessionIngressClient) PostSessionIngressEvent(
	ctx context.Context,
	sessionID string,
	event *SessionIngressEvent,
) error {
	endpoint := c.sessionEndpoint(sessionID, "events")

	// Build payload: {"events": [event]}
	payload := make(map[string]interface{})
	payload["events"] = []*SessionIngressEvent{event}

	// Log at Debug level: "posting session ingress event"
	// slog attrs: endpoint (string), session_id (string), event_type (string),
	//             use_v2 (bool/uint), event_data_type (type), payload (map)
	c.Logger.Debug("posting session ingress event",
		"endpoint", endpoint,
		"session_id", sessionID,
		"use_v2", c.UseV2,
	)

	err := c.postJSON(ctx, endpoint, payload, "session_event")
	if err != nil {
		return err
	}

	// Log at Warn level on success with nil error (unexpected path).
	c.Logger.Warn("posted session ingress event with nil error",
		"session_id", sessionID,
		"event_type", event.Type,
	)

	return nil
}

// PostForwardDiagLogs posts diagnostic logs to the session's "diag_logs" endpoint.
//
// If the logs slice is empty (R9 == nil), returns immediately.
// Converts DiagLogEntry slice to wire format via diagLogsToWireFormat.
// Builds payload: {"items": wireFormatEntries}
// Posts to sessionEndpoint with action "diag_logs" (9 bytes).
// Content-Type: "diag_logs" (9 = 0x09 bytes).
//
// Binary address: 0x830de0
func (c *HttpSessionIngressClient) PostForwardDiagLogs(
	ctx context.Context,
	sessionID string,
	logs []DiagLogEntry,
) error {
	endpoint := c.sessionEndpoint(sessionID, "diag_logs")

	wireEntries := diagLogsToWireFormat(logs, len(logs))

	payload := make(map[string]interface{})
	payload["items"] = wireEntries

	// Log at Warn level: "forwarding diag logs"
	// slog attrs: endpoint (string), session_id (string), num_logs (int)
	c.Logger.Warn("forwarding diag logs",
		"endpoint", endpoint,
		"session_id", sessionID,
		"num_logs", len(logs),
	)

	err := c.postJSON(ctx, endpoint, payload, "diag_logs")
	if err != nil {
		return err
	}

	// Log at Warn level on success fallthrough.
	c.Logger.Warn("posted diag logs with nil error",
		"session_id", sessionID,
		"num_logs", len(logs),
	)

	return nil
}

// PostSessionEvent posts a session activity event for the given category and event type.
// Binary address: 0x82fe00
func (c *HttpSessionIngressClient) PostSessionEvent(sessionID string, category LogCategory, eventType string) error {
	endpoint := c.sessionEndpoint(sessionID, "session_event")
	payload := map[string]interface{}{
		"category":   string(category),
		"event_type": eventType,
	}
	return c.postJSON(context.Background(), endpoint, payload, "session_event")
}

// PostSyntheticAssistantEvent posts a synthetic assistant message event.
// Binary address: 0x830200
func (c *HttpSessionIngressClient) PostSyntheticAssistantEvent(sessionID string, message string) error {
	endpoint := c.sessionEndpoint(sessionID, "synthetic_assistant")
	payload := map[string]interface{}{
		"message": message,
	}
	return c.postJSON(context.Background(), endpoint, payload, "synthetic_assistant")
}

// PostResultEvent posts a result event for the given category and event type.
// Binary address: 0x830600
func (c *HttpSessionIngressClient) PostResultEvent(sessionID string, category LogCategory, eventType string) error {
	endpoint := c.sessionEndpoint(sessionID, "result")
	payload := map[string]interface{}{
		"category":   string(category),
		"event_type": eventType,
	}
	return c.postJSON(context.Background(), endpoint, payload, "result")
}

// diagLogsToWireFormat converts a slice of DiagLogEntry to the wire format
// (slice of map[string]interface{}).
//
// For each entry:
//   - Formats Timestamp as "2006-01-02T15:04:05.000000000Z07:00" (29 = 0x1d bytes)
//   - Creates a new map with "timestamp" key set to the formatted time string
//   - Iterates over the entry's Fields map and copies key-value pairs into the new map
//
// Binary address: 0x831220
func diagLogsToWireFormat(logs []DiagLogEntry, count int) []map[string]interface{} {
	result := make([]map[string]interface{}, 0, count)

	for i := 0; i < count; i++ {
		entry := logs[i]

		// Each wire entry starts with a new map, sized for fields + 1 (timestamp).
		wireEntry := make(map[string]interface{}, len(entry.Fields)+1)

		// Format timestamp: "2006-01-02T15:04:05.000000000Z07:00"
		wireEntry["timestamp"] = entry.Timestamp.Format("2006-01-02T15:04:05.000000000Z07:00")

		// Copy all fields from the entry.
		for k, v := range entry.Fields {
			wireEntry[k] = v
		}

		result = append(result, wireEntry)
	}

	return result
}
