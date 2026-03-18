// Reconstructed from binary: Build ID a6f96673, Go 1.25.6
// Source: internal/api/get_session_context.go
//
// Original source path:
//   /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/api/get_session_context.go

package api

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/url"
)

// SessionsClient is the client for the sessions API (v2).
//
// Struct layout (from GetSessionContext disassembly):
//
//	0x00:      *HttpClient      (pointer to shared HTTP client)
//	0x08-0x17: ApiKey   string  (API key for authorization)
//	0x18:      Logger   *slog.Logger
type SessionsClient struct {
	Client *HttpClient  // offset 0x00
	ApiKey string       // offset 0x08 (string: ptr + len)
	Logger *slog.Logger // offset 0x18
}

// SessionContext is the response type from GetSessionContext.
// Fields inferred from JSON unmarshal and subsequent field access.
//
// The binary checks offset 0x50 (non-zero check, SETNE) and
// offset 0x60 (non-zero check, SETNE) after unmarshalling.
// These are likely boolean or optional fields determining session state.
type SessionContext struct {
	// String fields at offset 0x00-0x0f (first string field, e.g., session ID or type)
	ID string `json:"id"`
	// Additional fields...
	// offset 0x50: checked for non-nil (some slice/pointer field)
	// offset 0x60: checked for non-nil (some slice/pointer field)
}

// GetSessionContext retrieves the session context for a given session.
//
// Endpoint: GET "%s/v2/sessions/%s" (17 = 0x11 bytes format string)
// where %s is the base URL and the URL-escaped session ID.
//
// Sets headers:
//   - Authorization: Bearer <apiKey> (via setAuthHeader)
//   - X-Api-Key: <raw apiKey> (10 = 0x0a bytes header value)
//   - Anthropic-Beta: <beta string> (14 = 0x0e bytes header key, 18 = 0x12 bytes value)
//
// Uses RetryableHTTPDo for the HTTP call.
//
// Response handling:
//   - 200: JSON unmarshal into SessionContext, log at Debug level, return result
//   - 401: return error "session context request unauthorized for session %s, status %d, body: %s"
//   - 403: similar to 401
//   - 404: return error "session %s not found: %s" (24 = 0x18 bytes format)
//   - other: return error "unexpected status %d, body: %s" (49 = 0x31 bytes format)
//
// Binary address: 0x82e2e0
// Closure (func1): 0x82efe0
func (c *SessionsClient) GetSessionContext(
	ctx context.Context,
	sessionID string,
) (*SessionContext, error) {
	escapedSessionID := url.PathEscape(sessionID)

	// Build URL: "%s/v2/sessions/%s"
	endpoint := fmt.Sprintf("%s/v2/sessions/%s", c.Client.BaseURL, escapedSessionID)

	// Create GET request.
	req, err := http.NewRequestWithContext(ctx, "GET", endpoint, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create session context request: %w", err)
	}

	// Set authorization header.
	c.Client.setAuthHeader(req, c.ApiKey)

	// Set X-Api-Key header (value = apiKey, 10 bytes -> "x-api-key" canonical? or raw)
	req.Header.Set("X-Api-Key", c.ApiKey)

	// Set Anthropic-Beta header (14 bytes key, 18 bytes value).
	req.Header.Set("Anthropic-Beta", "interleaved-thinking")

	// Log at Debug level: "getting session context"
	// slog attrs: endpoint (string), session_id (string)
	c.Logger.Debug("getting session context",
		"endpoint", endpoint,
		"session_id", sessionID,
	)

	// Execute request with retries.
	resp, err := c.Client.RetryableHTTPDo(ctx, req, nil)
	if err != nil {
		return nil, fmt.Errorf("session context request failed: %w", err)
	}
	defer resp.Body.Close()

	// Read response body.
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read session context response: %w", err)
	}

	switch resp.StatusCode {
	case http.StatusOK: // 200 = 0xc8
		var sessionCtx SessionContext
		if err := json.Unmarshal(body, &sessionCtx); err != nil {
			return nil, fmt.Errorf("failed to unmarshal session context response: %w", err)
		}

		// Check boolean fields derived from session context.
		// offset 0x50: hasField1 (converted to bool via SETNE)
		// offset 0x60: hasField2 (converted to bool via SETNE)

		// Log at Debug level: "got session context"
		// slog attrs: endpoint (string), session_id (string),
		//             session_id (string, from response), has_field1 (bool), has_field2 (bool)
		c.Logger.Debug("got session context",
			"endpoint", endpoint,
			"session_id", sessionID,
			"response_id", sessionCtx.ID,
			"has_field1", sessionCtx.ID != "",
			"has_field2", false,
		)

		return &sessionCtx, nil

	case http.StatusUnauthorized, http.StatusForbidden: // 401 = 0x191, 403 = 0x193
		return nil, fmt.Errorf(
			"session context request unauthorized for session %s, status %d, body: %s",
			sessionID, resp.StatusCode, string(body),
		)

	case http.StatusNotFound: // 404 = 0x194
		return nil, fmt.Errorf("session %s not found: %s", sessionID, string(body))

	default:
		return nil, fmt.Errorf("unexpected status %d, body: %s", resp.StatusCode, string(body))
	}
}
