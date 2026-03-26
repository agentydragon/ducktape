// Reconstructed from binary: Build ID 64bc4dc1
// Source: internal/api/retry.go
//
// Original source path:
//   /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/api/retry.go

package api

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"log/slog"
	"math"
	"math/rand"
	"net/http"
	"time"
)

// calculateBackoff computes the backoff duration for a given retry attempt.
//
// If attempt <= 0, returns config.InitialDelay.
// Otherwise: base = config.InitialDelay * (config.BackoffFactor ^ (attempt-1)),
// capped at config.MaxDelay, then jittered by up to +30% (0.3 * rand.Float64()).
//
// The jitter factor 0.3 is encoded as IEEE754 float64 0x3fd3333333333333.
//
// Binary address: 0x82f140
func calculateBackoff(attempt int64, config *RetryConfig) time.Duration {
	if attempt <= 0 {
		return config.InitialDelay
	}

	base := float64(config.InitialDelay) * math.Pow(config.BackoffFactor, float64(attempt-1))
	maxDelay := float64(config.MaxDelay)
	if base > maxDelay {
		base = maxDelay
	}

	jitter := rand.Float64() * 0.3 * base
	total := base + jitter

	return time.Duration(int64(total))
}

// RetryableHTTPDo executes an HTTP request with automatic retries and exponential backoff.
//
// It retries on specific HTTP status codes: 408, 429, 500, 502, 503.
// For each retry attempt:
//  1. Clones the original request
//  2. Resets the request body (if body data was provided)
//  3. Logs the attempt with slog at Debug level
//  4. Executes the HTTP request via HttpClient.Client.Do
//  5. On retryable status or error, calculates backoff and waits
//  6. If context is cancelled during wait, returns context error
//
// Parameters:
//   - ctx: context for cancellation
//   - req: the original HTTP request (cloned on each attempt)
//   - retryConfig: optional override for the client's RetryConfig (may be nil)
//
// Returns the HTTP response on success, or an error after exhausting retries.
//
// Retryable status codes: 408, 429, 500, 502, 503
//
// Binary address: 0x82f200
func (c *HttpClient) RetryableHTTPDo(ctx context.Context, req *http.Request, retryConfig *RetryConfig) (*http.Response, error) {
	if retryConfig == nil {
		retryConfig = c.Retry
		if retryConfig == nil {
			retryConfig = &RetryConfig{
				MaxRetries:    3,
				InitialDelay:  1 * time.Second,
				MaxDelay:      10 * time.Second,
				BackoffFactor: 2.0,
			}
		}
	}

	// Read body data for replay on retries (if body exists).
	// Binary checks req.Body (offset 0x40/0x48) and reads via io.ReadAll.
	var bodyData []byte
	var bodyLen int64
	if req.Body != nil {
		var err error
		bodyData, err = io.ReadAll(req.Body)
		if err != nil {
			return nil, fmt.Errorf("error reading request body: %w", err)
		}
		req.Body.Close()
	}

	var lastErr error
	for attempt := int64(0); attempt <= retryConfig.MaxRetries; attempt++ {
		// Clone the request for each attempt.
		clonedReq := req.Clone(ctx)

		// Reset body if we have body data.
		if bodyData != nil {
			reader := bytes.NewReader(bodyData)
			clonedReq.Body = io.NopCloser(reader)
			clonedReq.ContentLength = int64(len(bodyData))
			_ = bodyLen // suppress unused
		}

		url := clonedReq.URL.String()

		// Log at Debug level: "retrying request"
		// slog attrs: attempt (int), max_retries (int), method (string), url (string)
		c.Logger.(*slog.Logger).Debug("retrying request",
			"attempt", attempt,
			"max_retries", retryConfig.MaxRetries,
			"method", clonedReq.Method,
			"url", url,
		)

		resp, err := c.Client.Do(clonedReq)
		if err != nil {
			lastErr = fmt.Errorf("request failed: %w", err)
		} else {
			statusCode := resp.StatusCode
			// Check if status is retryable: 408, 429, 500, 502, 503
			if statusCode == 408 || statusCode == 429 || statusCode == 500 ||
				statusCode == 502 || statusCode == 503 {
				lastErr = fmt.Errorf("retryable status code: %d", statusCode)
				resp.Body.Close()
			} else {
				// Non-retryable status or success: return immediately.
				return resp, nil
			}
		}

		// If we've exhausted retries, break.
		if attempt >= retryConfig.MaxRetries {
			break
		}

		// Calculate backoff and wait.
		backoffNs := calculateBackoff(attempt+1, retryConfig)
		backoffMs := backoffNs / time.Millisecond

		// Log at Warn level: "retrying after backoff"
		// slog attrs: attempt, backoff_seconds, max_retries, error (string), method, url
		c.Logger.(*slog.Logger).Warn("retrying after backoff",
			"attempt", attempt+1,
			"backoff_seconds", backoffMs,
			"max_retries", retryConfig.MaxRetries,
			"error", lastErr,
			"method", clonedReq.Method,
			"url", url,
		)

		timer := time.NewTimer(backoffNs)
		select {
		case <-timer.C:
			// Backoff elapsed, continue to next attempt.
		case <-ctx.Done():
			// Context cancelled during backoff wait.
			timer.Stop()
			return nil, fmt.Errorf("context cancelled while retrying: %w", ctx.Err())
		}
	}

	// All retries exhausted.
	return nil, fmt.Errorf("request failed after retries: %w", lastErr)
}
