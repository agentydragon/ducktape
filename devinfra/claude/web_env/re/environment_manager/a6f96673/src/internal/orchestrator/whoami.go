// Reconstructed from binary: environment-manager (Build ID a6f96673)
// Source: internal/orchestrator/whoami.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/internal/orchestrator

package orchestrator

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/util"
)

// WhoamiClient calls the /v1/environments/whoami endpoint to discover the
// identity of the current environment service key.
// Binary symbol: *orchestrator.WhoamiClient (interface type in nm output)
// Binary symbol: *orchestrator.WhoamiResponse (type in nm output)
type WhoamiClient struct {
	// Field layout (reconstructed from NewWhoamiClient at 0xa90f40):
	// Offset 0x00: apiBaseURL string (ptr + len)
	// Offset 0x10: sessionID string (ptr + len)
	// Offset 0x20: httpClient *http.Client
	// Offset 0x28: logger *slog.Logger
	APIBaseURL string
	SessionID  string
	HTTPClient *http.Client
	Logger     *slog.Logger
}

// WhoamiResponse is the response from the whoami endpoint.
// Contains identity information for the service key.
//
// Field layout (from GetIdentity disassembly at 0xa917bb-0xa91c94):
// Offset 0x10: session_id string
// Offset 0x20: org_id string
type WhoamiResponse struct {
	SessionID       string `json:"session_id"`
	OrgID           string `json:"org_id"`
	EnvironmentType string `json:"environment_type"`
}

// NewWhoamiClient creates a new WhoamiClient configured with the given API
// base URL, API key, session ID, and logger. It normalizes the API URL and
// sets up an HTTP client with appropriate timeouts.
//
// Binary: 0xa90f40 - orchestrator.NewWhoamiClient
// Source: orchestrator/whoami.go
//
// Parameters (register ABI):
//
//	AX = apiBaseURL string ptr
//	BX = apiBaseURL string len
//	CX = sessionID string len
//	DI = apiKey string ptr
//	SI = logger *slog.Logger
//
// Returns:
//
//	AX = *WhoamiClient
func NewWhoamiClient(
	apiBaseURL string,
	apiKey string,
	sessionID string,
	logger *slog.Logger,
) *WhoamiClient {
	// Validate/normalize API base URL.
	// Binary: 0xa90f77-0xa91005
	if !strings.HasPrefix(apiBaseURL, "http://") && !strings.HasPrefix(apiBaseURL, "https://") {
		apiBaseURL = "https://" + apiBaseURL
	}

	// Create logger with whoami attributes.
	// Binary: 0xa91014-0xa9108b
	whoamiLogger := logger.With(
		slog.String("component", "whoami_client"),
	)

	// Allocate WhoamiClient struct.
	// Binary: 0xa910a0 runtime.newobject
	client := &WhoamiClient{
		APIBaseURL: apiBaseURL,
		SessionID:  sessionID,
		Logger:     whoamiLogger,
		HTTPClient: &http.Client{
			Timeout: 30 * time.Second, // 0x6fc23ac00 ns
		},
	}

	_ = apiKey

	return client
}

// GetIdentity calls the /v1/environments/whoami endpoint and returns the
// identity response. It creates an HTTP GET request with appropriate headers,
// executes it, and parses the JSON response.
//
// Binary: 0xa911a0 - (*WhoamiClient).GetIdentity
// Source: orchestrator/whoami.go
//
// Assembly flow:
//  1. Build URL: fmt.Sprintf("%s/v1/environments/whoami", w.APIBaseURL)
//     URL format string: 0x19=25 chars "%s/v1/environments/whoami"
//  2. Log "Calling whoami endpoint" (0x17=23 chars) at Info level with 1 attr
//  3. http.NewRequestWithContext("GET", url, nil) at 0xa91348
//     If error: fmt.Errorf("failed to create whoami request: %w") (0x23=35 chars)
//  4. Set headers: Authorization (Bearer), Content-Type (application/json),
//     X-Environment-Manager-Version (util.Version)
//  5. Execute HTTP request via w.HTTPClient.Do at 0xa916cb
//     If error: fmt.Errorf("whoami request failed: %w") (0x19=25 chars)
//  6. defer resp.Body.Close (deferwrap1 at 0xa91d40)
//  7. io.ReadAll at 0xa917c0
//     If error: fmt.Errorf("failed to read whoami response body: %w") (0x27=39 chars)
//  8. Check status code, parse JSON into WhoamiResponse
func (w *WhoamiClient) GetIdentity(ctx context.Context) (*WhoamiResponse, error) {
	// Step 1: Build whoami URL.
	url := fmt.Sprintf("%s/v1/environments/whoami", w.APIBaseURL)

	// Step 2: Log the request.
	w.Logger.Info("Calling whoami endpoint",
		"url", url,
	)

	// Step 3: Create HTTP GET request.
	req, err := http.NewRequestWithContext(ctx, "GET", url, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create whoami request: %w", err)
	}

	// Step 4: Set request headers.
	authValue := fmt.Sprintf("Bearer %s", w.SessionID)
	req.Header.Set("Authorization", authValue)
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Environment-Manager-Version", util.Version)

	// Step 5: Execute HTTP request.
	resp, err := w.HTTPClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("whoami request failed: %w", err)
	}
	defer resp.Body.Close()

	// Step 6: Read response body.
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read whoami response body: %w", err)
	}

	// Step 7: Check status code.
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("whoami returned status %d: %s", resp.StatusCode, string(body))
	}

	// Step 8: Parse response JSON.
	var whoamiResp WhoamiResponse
	if err := json.Unmarshal(body, &whoamiResp); err != nil {
		return nil, fmt.Errorf("failed to parse whoami response: %w", err)
	}

	return &whoamiResp, nil
}
