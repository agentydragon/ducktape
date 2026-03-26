// Reconstructed from binary: environment-manager (Build ID 64bc4dc1)
// Source: internal/orchestrator/poller.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/internal/orchestrator

package orchestrator

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"math/rand"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/util"
)

// Poller implements PollerInterface and handles polling the API for available
// sessions with configurable intervals and jitter.
type Poller struct {
	// Field layout (reconstructed from NewPollerWithWorkerID at 0xa8f160):
	// Offset 0x00: apiBaseURL string (ptr + len) - the base URL for API calls
	// Offset 0x10: sessionID string (ptr + len)
	// Offset 0x20: apiKey string (ptr + len)
	// Offset 0x30: workerID string (ptr + len)
	// Offset 0x40: maxSessions int64 (checked at 0xa8f7b1)
	// Offset 0x48: httpClient *http.Client
	// Offset 0x50: logger *slog.Logger
	APIBaseURL  string
	SessionID   string
	APIKey      string
	WorkerID    string
	MaxSessions int64
	HTTPClient  *http.Client
	Logger      *slog.Logger
}

// NewPollerWithWorkerID creates a new Poller with the specified configuration.
// If the workerID is empty, it defaults to the hostname. If the API base URL
// doesn't start with "http://" or "https://", "https://" is prepended.
//
// Binary: 0xa8f160 - orchestrator.NewPollerWithWorkerID
// Source: orchestrator/poller.go
func NewPollerWithWorkerID(
	apiBaseURL string,
	sessionID string,
	apiKey string,
	secretPath string,
	workerID string,
	logger *slog.Logger,
) *Poller {
	// If workerID is empty, default to hostname.
	// Binary: 0xa8f1c0-0xa8f234
	if workerID == "" {
		hostname, err := os.Hostname()
		if err == nil {
			workerID = hostname
		}
	}

	// Validate/normalize the API base URL.
	// Binary: 0xa8f241-0xa8f2ee
	if !strings.HasPrefix(apiBaseURL, "http://") && !strings.HasPrefix(apiBaseURL, "https://") {
		apiBaseURL = "https://" + apiBaseURL
	}

	// Create logger with poller-specific attributes.
	// Binary: 0xa8f2fb-0xa8f4a0
	args := []any{
		slog.String("component", "poller"),
		slog.String("api_base_url", apiBaseURL),
	}
	if workerID != "" {
		args = append(args, slog.String("worker_id", workerID))
	}
	pollerLogger := logger.With(args...)

	// Allocate and populate the Poller struct.
	// Binary: 0xa8f4c2 runtime.newobject
	poller := &Poller{
		APIBaseURL: apiBaseURL,
		SessionID:  sessionID,
		APIKey:     apiKey,
		WorkerID:   workerID,
		Logger:     pollerLogger,
		HTTPClient: &http.Client{},
	}

	return poller
}

// Poll executes a single poll request to the API, checking for available
// sessions or work items. It creates an HTTP GET request with appropriate
// headers (Authorization, Content-Type, X-Environment-Manager-Version,
// and optionally X-Worker-Id), executes it, and parses the response.
//
// Binary: 0xa8f660 - (*Poller).Poll
// Source: orchestrator/poller.go
//
// Assembly flow:
//  1. Log "Starting poll for available sessions" (0x23=35 chars) at Debug level (-4)
//  2. context.WithTimeout with 30s (0x6fc23ac00 ns), defer cancel
//  3. Build URL: fmt.Sprintf("Poll %s with worker_id %s", p.APIBaseURL, p.SessionID)
//     If p.MaxSessions > 0: append fmt.Sprintf(" (max_sessions: %d)", p.MaxSessions)
//  4. http.NewRequestWithContext("GET", url, nil)
//  5. Set headers on request:
//     - "Authorization": fmt.Sprintf("Bearer %s", p.APIKey) (0x09=9 chars key)
//     - "Content-Type": "application/json" (0x17=23 chars value, 0x0e=14 chars key)
//     - "X-Environment-Manager-Version": util.Version (0x1c=28 chars key)
//     - "X-Worker-Id": p.WorkerID (0x13=19 chars key, only if workerID non-empty)
//  6. time.Now() before HTTP call
//  7. p.HTTPClient.do(request)
//  8. Record time.Since for duration
//  9. Parse response: check status code, read body, unmarshal JSON
func (p *Poller) Poll(ctx context.Context) (*SessionResponse, error) {
	// Step 1: Log at Debug level.
	p.Logger.Debug("Starting poll for available sessions")

	// Step 2: Create context with 30-second timeout.
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()

	// Step 3: Build poll URL.
	pollMsg := fmt.Sprintf("Poll %s with worker_id %s", p.APIBaseURL, p.SessionID)
	if p.MaxSessions > 0 {
		pollMsg += fmt.Sprintf(" (max_sessions: %d)", p.MaxSessions)
	}

	// Step 4: Create HTTP request.
	req, err := http.NewRequestWithContext(ctx, "GET", pollMsg, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create poll request: %w", err)
	}

	// Step 5: Set request headers.
	// Authorization header with Bearer token.
	authValue := fmt.Sprintf("Bearer %s", p.APIKey)
	req.Header.Set("Authorization", authValue)

	// Content-Type header.
	req.Header.Set("Content-Type", "application/json")

	// X-Environment-Manager-Version header.
	req.Header.Set("X-Environment-Manager-Version", util.Version)

	// X-Worker-Id header (only if workerID is non-empty).
	// Binary: 0xa8fc02-0xa8fd08 - checks p.WorkerID (offset 0x38) length
	if p.WorkerID != "" {
		req.Header.Set("X-Worker-Id", p.WorkerID)
	}

	// Step 6: Record start time.
	startTime := time.Now()

	// Step 7: Execute HTTP request.
	resp, err := p.HTTPClient.Do(req)

	// Step 8: Record duration.
	duration := time.Since(startTime)
	durationMs := duration.Milliseconds()

	if err != nil {
		p.Logger.Error("Poll request failed",
			"duration_ms", durationMs,
			"error", err,
		)
		return nil, fmt.Errorf("poll request failed: %w", err)
	}
	defer resp.Body.Close()

	// Step 9: Read response headers for retry-after and request-id.
	// Binary: reads "retry-after" and "Retry-After", "x-request-id"
	retryAfter := resp.Header.Get("Retry-After")
	if retryAfter == "" {
		retryAfter = resp.Header.Get("retry-after")
	}
	requestID := resp.Header.Get("X-Request-Id")

	// Step 10: Check response status code.
	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		p.Logger.Error("Poll request returned non-200 status",
			"status_code", resp.StatusCode,
			"duration_ms", durationMs,
			"retry_after", retryAfter,
			"request_id", requestID,
			"body", string(body),
		)
		return nil, fmt.Errorf("poll returned status %d: %s", resp.StatusCode, string(body))
	}

	// Step 11: Read and parse response body.
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, fmt.Errorf("failed to read poll response body: %w", err)
	}

	if len(body) == 0 {
		// No session available.
		p.Logger.Debug("Poll returned empty response",
			"duration_ms", durationMs,
			"request_id", requestID,
		)
		return nil, nil
	}

	// Parse session response.
	var session SessionResponse
	if err := json.Unmarshal(body, &session); err != nil {
		return nil, fmt.Errorf("failed to parse poll response: %w", err)
	}

	p.Logger.Info("Poll returned session",
		"duration_ms", durationMs,
		"request_id", requestID,
	)

	return &session, nil
}

// SleepWithJitter sleeps for the poll interval with random jitter to
// prevent thundering herd effects. The jitter is up to 20% of the interval.
//
// Binary: 0xa90d20 - (*Poller).SleepWithJitter
// Source: orchestrator/poller.go
func (p *Poller) SleepWithJitter(ctx context.Context) error {
	// Add up to 20% jitter to the default poll interval.
	jitter := time.Duration(rand.Int63n(int64(defaultPollInterval / 5)))
	sleepDuration := defaultPollInterval + jitter

	p.Logger.Debug("Sleeping with jitter",
		"sleep_duration", sleepDuration,
	)

	select {
	case <-time.After(sleepDuration):
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Default poll interval constant.
const defaultPollInterval = 5 * time.Minute // 0x45d964b800 nanoseconds

// Unused import guards.
var _ = strconv.Itoa
