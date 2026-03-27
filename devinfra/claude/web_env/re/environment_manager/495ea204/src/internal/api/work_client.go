// Reconstructed from binary: Build ID 495ea204
// Source: internal/api/work_client.go
//
// Original source path:
//   /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/api/work_client.go

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

// WorkClient is the client for the work/environments API (v1).
//
// Struct layout (from AcknowledgeWork disassembly):
//
//	0x00:      *HttpClient     (pointer, dereferenced at 0x831f75)
//	0x08-0x17: ApiKey string   (used in setAuthHeader and auth context error)
//	0x18:      Logger *slog.Logger (not directly visible but inferred from pattern)
//
// Additional known work endpoints from strings:
//
//	"%s/v1/environments/%s/work/poll"         (work polling)
//	"%s/v1/environments/%s/work/%s/ack"       (acknowledge work)
//	"%s/v1/environments/%s/work/%s/heartbeat" (heartbeat)
//	"%s/v1/environments/%s/work/%s/stop"      (stop work)
type WorkClient struct {
	Client *HttpClient  // offset 0x00
	ApiKey string       // offset 0x08 (string: ptr + len)
	Logger *slog.Logger // offset 0x18
}

// AcknowledgeWork sends an acknowledgement for a work item to the API.
//
// Endpoint: PUT "%s/v1/environments/%s/work/%s/ack" (33 = 0x21 bytes format string)
// Arguments: base URL, URL-escaped environment ID, URL-escaped work ID.
//
// Both environmentID and workID are URL-escaped via url.PathEscape (net/url.escape mode 2).
//
// Sets headers:
//   - Authorization: Bearer <apiKey> (via setAuthHeader)
//   - X-Api-Key: <apiKey> (value = apiKey string from WorkClient)
//   - Content-Type: application/json
//   - X-Environment-Manager-Version: <version>
//
// Logs at Debug level with: endpoint, environment_id, work_id
//
// Response handling:
//   - On request creation error: returns "cannot ACK work %s: request error: %w" (32 = 0x20 bytes)
//   - On HTTP error: reads response body and returns error with details
//   - On success: JSON unmarshals response body into result struct
//   - Handles missing auth context: "cannot ACK work %s: missing auth context" (found in strings)
//
// Binary address: 0x831ea0
// Closure (func1): 0x8329c0
func (c *WorkClient) AcknowledgeWork(
	ctx context.Context,
	environmentID string,
	workID string,
) error {
	// URL-escape both IDs.
	escapedEnvID := url.PathEscape(environmentID)
	escapedWorkID := url.PathEscape(workID)

	// Build endpoint URL.
	endpoint := fmt.Sprintf("%s/v1/environments/%s/work/%s/ack",
		c.Client.BaseURL,
		escapedEnvID,
		escapedWorkID,
	)

	// Create PUT request with no body.
	req, err := http.NewRequestWithContext(ctx, "PUT", endpoint, nil)
	if err != nil {
		return fmt.Errorf("cannot ACK work %s: request error: %w", workID, err)
	}

	// Set authorization header using WorkClient's API key.
	c.Client.setAuthHeader(req, c.ApiKey)

	// Set X-Api-Key header with the raw API key.
	req.Header.Set("X-Api-Key", c.ApiKey)

	// Set Content-Type header.
	req.Header.Set("Content-Type", "application/json")

	// Set version header.
	// req.Header.Set("X-Environment-Manager-Version", util.Version)

	// Log at Debug level.
	c.Logger.Debug("acknowledging work",
		"endpoint", endpoint,
		"environment_id", environmentID,
		"work_id", workID,
	)

	// Execute with retries.
	resp, err := c.Client.RetryableHTTPDo(ctx, req, nil)
	if err != nil {
		return fmt.Errorf("cannot ACK work %s: %w", workID, err)
	}
	defer resp.Body.Close()

	// Read response body.
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return fmt.Errorf("cannot ACK work %s: failed to read response: %w", workID, err)
	}

	if resp.StatusCode != http.StatusOK {
		// Handle non-200 responses.
		// Checks for specific status codes (401, 403 for auth errors).
		if resp.StatusCode == http.StatusUnauthorized || resp.StatusCode == http.StatusForbidden {
			return fmt.Errorf(
				"cannot ACK work %s: unauthorized, status %d, body: %s",
				workID, resp.StatusCode, string(body),
			)
		}
		if resp.StatusCode == http.StatusNotFound {
			return fmt.Errorf(
				"cannot ACK work %s: not found, body: %s",
				workID, string(body),
			)
		}
		return fmt.Errorf(
			"cannot ACK work %s: unexpected status %d, body: %s",
			workID, resp.StatusCode, string(body),
		)
	}

	// On 200: unmarshal response.
	var result interface{}
	if err := json.Unmarshal(body, &result); err != nil {
		return fmt.Errorf("cannot ACK work %s: failed to unmarshal response: %w", workID, err)
	}

	// Log success at Debug level.
	c.Logger.Debug("acknowledged work",
		"endpoint", endpoint,
		"environment_id", environmentID,
		"work_id", workID,
	)

	return nil
}
