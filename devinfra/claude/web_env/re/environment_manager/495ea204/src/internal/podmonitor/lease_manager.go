// Reconstructed from a6f96673 DWARF extraction, carried forward to 495ea204.
// Package: internal/podmonitor
// Source: internal/podmonitor/lease_manager.go

package podmonitor

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"time"
)

// stopAPIError represents an error response from the stop API.
//
// Struct layout (from type equality at 0xad8680 and Error method at 0xad7480):
//
//	offset 0x00: StatusCode int
//	offset 0x08: Message string (ptr + len)
//
// Implements: error (itab at 0xf5b6a0)
type stopAPIError struct {
	StatusCode int    // offset 0x00
	Message    string // offset 0x08
}

// Error returns the error message string.
//
// Binary address: 0xad7480
func (e *stopAPIError) Error() string {
	return e.Message
}

// LeaseManager manages the heartbeat loop and lease lifecycle for an environment pod.
// It periodically sends heartbeats to the API, monitors lease expiry, and manages
// graceful shutdown when the lease expires or heartbeats fail permanently.
//
// Struct layout (from NewLeaseManager at 0xad42a0 and field access patterns):
//
//	offset 0x00:  environmentID string (ptr + len)
//	offset 0x10:  logger *slog.Logger
//	offset 0x18:  heartbeatInterval time.Duration
//	offset 0x20:  leaseDuration time.Duration
//	offset 0x28:  workID string (ptr + len)
//	offset 0x38:  sessionID string (ptr + len)
//	offset 0x48:  apiBaseURL string (ptr + len)
//	offset 0x58:  sessionIngressToken string (ptr + len)
//	offset 0x68:  healthFilePath string (ptr + len)
//	offset 0x70:  defaultHeartbeatInterval time.Duration (default: 600s = 10min)
//	offset 0x78:  mu sync.RWMutex (size ~0x18)
//	offset 0x90:  leaseExpiresAt string (ptr + len) - protected by mu
//	offset 0xa0:  consecutiveFailures int - protected by mu
//	offset 0xa8:  lastHeartbeatError string (ptr + len) - protected by mu
//	offset 0xb0:  lastError error (interface) - protected by mu
//	offset 0xc0:  gracePeriod time.Duration - protected by mu
//	offset 0xc8:  stuckThresholdSeconds string (ptr + len) - protected by mu
//	offset 0xd0:  expectedLastHeartbeat string (ptr + len) - protected by mu
//	offset 0x120: apiClient interface (type+data) - for heartbeat/stop calls
//	offset 0x130: ctx context.Context (interface: type+data)
//	offset 0x138: ctxValue interface
//	offset 0x140: cancel context.CancelFunc
//	offset 0x148: done chan struct{}
type LeaseManager struct {
	environmentID            string        // offset 0x00
	logger                   *slog.Logger  // offset 0x10
	heartbeatInterval        time.Duration // offset 0x18
	leaseDuration            time.Duration // offset 0x20
	workID                   string        // offset 0x28
	sessionID                string        // offset 0x38
	apiBaseURL               string        // offset 0x48
	sessionIngressToken      string        // offset 0x58
	healthFilePath           string        // offset 0x68
	defaultHeartbeatInterval time.Duration // offset 0x70

	mu                    sync.RWMutex  // offset 0x78
	leaseExpiresAt        string        // offset 0x90
	consecutiveFailures   int           // offset 0xa0
	lastHeartbeatError    string        // offset 0xa8
	lastError             error         // offset 0xb0
	gracePeriod           time.Duration // offset 0xc0
	stuckThresholdSeconds string        // offset 0xc8
	expectedLastHeartbeat string        // offset 0xd0

	httpClient *http.Client       // offset 0x120 area (simplified)
	ctx        context.Context    // offset 0x130
	cancel     context.CancelFunc // offset 0x140
	done       chan struct{}      // offset 0x148
}

// GetDefaultHealthFilePath returns the default path for the health file.
// The path is $HOME/.agent-health/envmgr-healthy (or /root/.agent-health/envmgr-healthy
// if $HOME is not set).
//
// Binary address: 0xad3a20
// Source file: lease_manager.go
func GetDefaultHealthFilePath() string {
	home, err := os.UserHomeDir()
	if err != nil {
		home = "/root"
	}
	return filepath.Join(home, ".agent-health/envmgr-healthy")
}

// isPermanentHeartbeatError checks whether a heartbeat error is permanent
// (i.e., retrying won't help). It checks the error message against known
// permanent HTTP status codes and status text.
//
// Permanent error indicators (first group - checked via string contains):
//
//	"124" (timeout), "403", "404", "412"
//	"Unauthorized", "Forbidden", "Precondition Failed"
//
// Additional checked codes (second group - if not matched in first):
//
//	"429", "500", "502"
//
// Binary address: 0xad3ae0
// Source file: lease_manager.go
func isPermanentHeartbeatError(err error) bool {
	if err == nil {
		return false
	}

	errMsg := err.Error()

	// First group: definitely permanent errors
	permanentIndicators := []string{
		"124", "403", "404", "412",
		"Unauthorized", "Forbidden", "Precondition Failed",
	}
	for _, indicator := range permanentIndicators {
		if strings.Contains(errMsg, indicator) {
			return true
		}
	}

	// Second group: server errors that might be transient but checked separately
	transientServerErrors := []string{
		"429", "500", "502",
	}
	for _, indicator := range transientServerErrors {
		if strings.Contains(errMsg, indicator) {
			return false
		}
	}

	// Default: not permanent (will retry)
	return false
}

// isPreconditionFailedError checks whether the error indicates a
// 412 Precondition Failed response.
//
// Binary address: 0xad3ec0
// Source file: lease_manager.go
func isPreconditionFailedError(err error) bool {
	if err == nil {
		return false
	}

	errMsg := err.Error()

	indicators := []string{
		"412",
		"Precondition Failed",
	}
	for _, indicator := range indicators {
		if strings.Contains(errMsg, indicator) {
			return true
		}
	}

	return false
}

// isRetriableStopError checks whether a stop API error should be retried.
// Context cancellation and deadline exceeded errors are not retriable.
// HTTP errors with status >= 500 are retriable.
// All other errors are retriable.
//
// Binary address: 0xad74a0
// Source file: lease_manager.go
func isRetriableStopError(err error) bool {
	// Context errors are not retriable
	if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
		return false
	}

	// Check for stopAPIError with status code
	var apiErr *stopAPIError
	if errors.As(err, &apiErr) {
		// Server errors (5xx) are retriable
		return apiErr.StatusCode >= 500
	}

	// All other errors are retriable
	return true
}

// NewLeaseManager creates a new LeaseManager with the given configuration.
//
// Parameters (from register mapping in disassembly):
//
//	AX: environmentID string ptr
//	BX: environmentID string len
//	CX: logger *slog.Logger
//	DI: heartbeatInterval time.Duration
//	SI: leaseDuration time.Duration
//	R8: apiClient (interface data)
//	R9: workID string ptr
//	R10: sessionID string ptr
//	R11: apiBaseURL string ptr
//	(stack): additional string lengths and remaining fields
//
// Binary address: 0xad42a0
// Source file: lease_manager.go
func NewLeaseManager(
	environmentID string,
	logger *slog.Logger,
	heartbeatInterval time.Duration,
	leaseDuration time.Duration,
	workID string,
	sessionID string,
	apiBaseURL string,
	sessionIngressToken string,
	healthFilePath string,
	httpClient *http.Client,
) *LeaseManager {
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})

	lm := &LeaseManager{
		environmentID:            environmentID,
		logger:                   logger,
		heartbeatInterval:        heartbeatInterval,
		leaseDuration:            leaseDuration,
		workID:                   workID,
		sessionID:                sessionID,
		apiBaseURL:               apiBaseURL,
		sessionIngressToken:      sessionIngressToken,
		healthFilePath:           healthFilePath,
		defaultHeartbeatInterval: 10 * time.Minute, // 0x8bb2c97000 ns = 600s
		httpClient:               httpClient,
		ctx:                      ctx,
		cancel:                   cancel,
		done:                     done,
	}

	return lm
}

// Start begins the lease manager: sends an initial heartbeat, schedules
// the lease expiry check, and starts the heartbeat loop goroutine.
//
// Binary address: 0xad4500
// Source file: lease_manager.go
//
// Closures:
//
//	gowrap1 at 0xad47c0 - goroutine wrapper for heartbeatLoop
func (lm *LeaseManager) Start(ctx context.Context) error {
	// Send initial heartbeat
	err := lm.sendHeartbeat()
	if err != nil {
		return fmt.Errorf("failed to obtain initial lease: %w", err)
	}

	// Log the start with heartbeat and lease duration info
	heartbeatSecs := lm.heartbeatInterval.Seconds()
	leaseSecs := lm.leaseDuration.Seconds()

	lm.logger.Info("lease_manager_started",
		"environment_id", lm.environmentID,
		"heartbeat_interval_seconds", heartbeatSecs,
		"lease_duration_seconds", leaseSecs,
	)

	// Schedule the lease expiry check
	lm.scheduleLeaseExpiryCheck()

	// Start the heartbeat loop in a goroutine
	go lm.heartbeatLoop()

	return nil
}

// Stop cancels the lease manager context and waits for the heartbeat loop
// to finish.
//
// Binary address: 0xad71c0
// Source file: lease_manager.go
func (lm *LeaseManager) Stop() {
	lm.cancel()
	<-lm.done
}

// Context returns the lease manager's context.
//
// Binary address: 0xad7400
// Source file: lease_manager.go
func (lm *LeaseManager) Context() context.Context {
	return lm.ctx
}

// GetGracePeriod returns the current grace period duration.
//
// Binary address: 0xad4160
// Source file: lease_manager.go
//
// Closure:
//
//	deferwrap1 at 0xad4240 - deferred RWMutex read-unlock
func (lm *LeaseManager) GetGracePeriod() time.Duration {
	lm.mu.RLock()
	defer lm.mu.RUnlock()
	return lm.gracePeriod
}

// GetLeaseInfo returns the current lease expiry time and consecutive failure count.
//
// Binary address: 0xad7220
// Source file: lease_manager.go
//
// Closure:
//
//	deferwrap1 at 0xad73a0 - deferred RWMutex read-unlock
func (lm *LeaseManager) GetLeaseInfo() (string, int) {
	lm.mu.RLock()
	defer lm.mu.RUnlock()
	return lm.leaseExpiresAt, int(lm.consecutiveFailures)
}

// setGracePeriodAndCancel sets the grace period and schedules context cancellation
// after the grace period expires.
//
// Binary address: 0xad4040
// Source file: lease_manager.go
//
// Closure:
//
//	func1 at 0xad40a0 - timer callback that calls cancel
func (lm *LeaseManager) setGracePeriodAndCancel(gracePeriod time.Duration) {
	lm.mu.Lock()
	lm.gracePeriod = gracePeriod
	lm.mu.Unlock()

	// Schedule cancellation after grace period
	time.AfterFunc(gracePeriod, func() {
		lm.cancel()
	})
}

// timeUntilExpiry computes the time remaining until the lease expires.
//
// Binary address: 0xad4f00
// Source file: lease_manager.go
//
// Closure:
//
//	deferwrap1 at 0xad5060 - deferred RWMutex read-unlock
func (lm *LeaseManager) timeUntilExpiry() time.Duration {
	lm.mu.RLock()
	defer lm.mu.RUnlock()

	if lm.leaseExpiresAt == "" {
		return 0
	}

	expiresAt, err := time.Parse(time.RFC3339, lm.leaseExpiresAt)
	if err != nil {
		return 0
	}

	return time.Until(expiresAt)
}

// heartbeatLoop is the main loop that periodically sends heartbeats.
// It runs until the context is cancelled. On heartbeat failure:
//   - Permanent errors trigger an immediate stop (callStopAPI + cancel)
//   - Transient errors are logged and retried
//
// Binary address: 0xad5500
// Source file: lease_manager.go
//
// Closures:
//
//	deferwrap1 at 0xad5fc0 - deferred close(done) channel
//	deferwrap2 at 0xad5e00 - deferred ticker.Stop()
//	func1 at 0xad5e60 - deferred updateHealthFile call
func (lm *LeaseManager) heartbeatLoop() {
	defer close(lm.done)
	defer lm.updateHealthFile()

	ticker := time.NewTicker(lm.heartbeatInterval)
	defer ticker.Stop()

	for {
		select {
		case <-lm.ctx.Done():
			return
		case <-ticker.C:
		}

		// Check if context is done before sending heartbeat
		if err := lm.ctx.Err(); err != nil {
			return
		}

		// Send heartbeat
		err := lm.sendHeartbeat()
		if err == nil {
			// Success - reset failure count and continue
			continue
		}

		// Heartbeat failed
		if isPermanentHeartbeatError(err) {
			// Permanent failure - log and initiate shutdown
			lm.logger.Error("permanent_heartbeat_failure",
				"environment_id", lm.environmentID,
				"error", err,
			)

			// Read the consecutive failure count and stop status
			lm.mu.RLock()
			stuckThreshold := lm.stuckThresholdSeconds
			expectedHeartbeat := lm.expectedLastHeartbeat
			lm.mu.RUnlock()

			if isPreconditionFailedError(err) {
				lm.logger.Warn("heartbeat_precondition_failed",
					"message", "heartbeat failed: HTTP 412",
				)
				lm.logger.Warn("heartbeat_rejected_lease_not_extended",
					"stuck_threshold_seconds", stuckThreshold,
					"expected_last_heartbeat", expectedHeartbeat,
				)
			} else {
				lm.logger.Error("heartbeat_failed_stopping_state_immediate_shutdown",
					"environment_id", lm.environmentID,
					"error", err,
				)
			}

			// Call stop API and cancel context
			lm.callStopAPI(lm.ctx, false)
			lm.cancel()
			return
		}

		// Transient failure - log warning and continue
		lm.logger.Warn("transient_heartbeat_failure",
			"environment_id", lm.environmentID,
			"error", err,
			"consecutive_failures", lm.consecutiveFailures,
		)
	}
}

// sendHeartbeat sends a single heartbeat request to the API.
// It constructs the heartbeat URL, sends the request with auth headers,
// and updates the lease state from the response.
//
// Binary address: 0xad6020
// Source file: lease_manager.go
//
// Closure:
//
//	deferwrap1 at 0xad7160 - deferred response body close
func (lm *LeaseManager) sendHeartbeat() error {
	now := time.Now()

	// Build heartbeat URL:
	// {apiBaseURL}/v1/environments/{environmentID}/work/{workID}/heartbeat
	heartbeatURL := fmt.Sprintf(
		"%s/v1/environments/%s/work/%s/heartbeat",
		lm.apiBaseURL,
		lm.workID,
		lm.environmentID,
	)

	// Get session ingress token (with read lock)
	lm.mu.RLock()
	sessionToken := lm.sessionIngressToken // might be replaced
	orgUUID := lm.expectedLastHeartbeat    // reused field
	lm.mu.RUnlock()

	// Build request headers
	headers := map[string][]string{
		"organization_uuid": {orgUUID},
	}
	if sessionToken == "" {
		sessionToken = "NO_HEARTBEAT"
	}
	headers["expected_last_heartbeat"] = []string{sessionToken}

	// Send the heartbeat request
	req, err := http.NewRequestWithContext(lm.ctx, http.MethodPost, heartbeatURL, nil)
	if err != nil {
		return fmt.Errorf("heartbeat RPC failed: %w", err)
	}

	for key, values := range headers {
		for _, v := range values {
			req.Header.Add(key, v)
		}
	}

	resp, err := lm.httpClient.Do(req)
	if err != nil {
		return fmt.Errorf("heartbeat RPC failed: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("heartbeat failed: HTTP %d", resp.StatusCode)
	}

	// Parse response and update lease state
	var heartbeatResp struct {
		LeaseExpiresAt        string `json:"lease_expires_at"`
		GracePeriod           int    `json:"grace_period"`
		StuckThresholdSeconds string `json:"stuck_threshold_seconds"`
		ExpectedLastHeartbeat string `json:"expected_last_heartbeat"`
	}

	if err := json.NewDecoder(resp.Body).Decode(&heartbeatResp); err != nil {
		return fmt.Errorf("heartbeat response: %w", err)
	}

	// Update lease state under write lock
	lm.mu.Lock()
	lm.leaseExpiresAt = heartbeatResp.LeaseExpiresAt
	lm.consecutiveFailures = 0
	lm.stuckThresholdSeconds = heartbeatResp.StuckThresholdSeconds
	lm.expectedLastHeartbeat = heartbeatResp.ExpectedLastHeartbeat
	lm.mu.Unlock()

	// Log successful heartbeat
	lm.logger.Debug("heartbeat_successful",
		"heartbeat_time", now.Format(time.RFC3339),
	)

	// Update health file
	lm.updateHealthFile()

	return nil
}

// scheduleLeaseExpiryCheck schedules a timer that fires when the lease is
// approaching expiry, triggering a graceful shutdown.
//
// Binary address: 0xad50c0
// Source file: lease_manager.go
//
// Closures:
//
//	func1 at 0xad53a0 - timer callback for lease expiry
//	deferwrap1 at 0xad54a0 - deferred cleanup
func (lm *LeaseManager) scheduleLeaseExpiryCheck() {
	ttl := lm.timeUntilExpiry()
	if ttl <= 0 {
		return
	}

	// Schedule check at some fraction of the TTL
	time.AfterFunc(ttl, func() {
		remaining := lm.timeUntilExpiry()
		if remaining <= 0 {
			lm.logger.Warn("lease_approaching_expiry_shutdown",
				"lease_expires_at", lm.leaseExpiresAt,
			)
			lm.cancel()
		}
	})
}

// scheduleClaudeCodeStuckCheck schedules a periodic check for stuck Claude Code
// processes. If a stuck process is detected, it initiates shutdown.
//
// Binary address: 0xad4820
// Source file: lease_manager.go
//
// Closures:
//
//	func1 at 0xad4be0 - timer callback for stuck check
//	deferwrap1 at 0xad4ea0 - deferred cleanup
func (lm *LeaseManager) scheduleClaudeCodeStuckCheck() {
	// Check if stuck threshold is configured
	lm.mu.RLock()
	threshold := lm.stuckThresholdSeconds
	lm.mu.RUnlock()

	if threshold == "" {
		return
	}

	// Schedule periodic stuck checks
	time.AfterFunc(lm.heartbeatInterval, func() {
		lm.logger.Info("stuck_check",
			"stuck_threshold_seconds", threshold,
		)
		// Re-schedule
		lm.scheduleClaudeCodeStuckCheck()
	})
}

// callStopAPI calls the stop API to signal that this environment should be stopped.
// If forced is true, it uses a forced stop; otherwise, a graceful stop.
//
// Binary address: 0xad75a0
// Source file: lease_manager.go
//
// Closures:
//
//	func1 at 0xad7960 - retry loop body
//	func1.deferwrap1 at 0xad8040 - deferred response body close
func (lm *LeaseManager) callStopAPI(ctx context.Context, forced bool) {
	lm.logger.Info("calling_stop_api",
		"forced", forced,
	)

	// Build stop URL
	stopURL := fmt.Sprintf(
		"%s/v1/environments/%s/work/%s/stop",
		lm.apiBaseURL,
		lm.workID,
		lm.environmentID,
	)

	// Retry loop
	maxRetries := 3
	for attempt := 0; attempt < maxRetries; attempt++ {
		req, err := http.NewRequestWithContext(ctx, http.MethodPost, stopURL, nil)
		if err != nil {
			continue
		}

		resp, err := lm.httpClient.Do(req)
		if err != nil {
			if !isRetriableStopError(err) {
				return
			}
			continue
		}
		resp.Body.Close()

		if resp.StatusCode >= 200 && resp.StatusCode < 300 {
			return // Success
		}

		apiErr := &stopAPIError{
			StatusCode: resp.StatusCode,
			Message:    fmt.Sprintf("stop API call failed: HTTP %d", resp.StatusCode),
		}

		if !isRetriableStopError(apiErr) {
			return
		}
	}
}

// CallStopAPIForced calls the stop API with the forced flag set to true.
//
// Binary address: 0xad7420
// Source file: lease_manager.go
func (lm *LeaseManager) CallStopAPIForced(ctx context.Context) {
	lm.callStopAPI(ctx, true)
}

// updateHealthFile writes the current health status to a JSON file at the
// configured health file path. The file contains:
//   - timestamp: current time in RFC3339 format
//   - lease_expires_at: when the lease expires
//   - consecutive_failures: number of consecutive heartbeat failures
//   - grace_period: remaining grace period
//   - fires_in_seconds: seconds until lease expiry
//
// Binary address: 0xad80a0
// Source file: lease_manager.go
func (lm *LeaseManager) updateHealthFile() {
	if lm.healthFilePath == "" {
		return
	}

	// Read current state under read lock
	lm.mu.RLock()
	leaseExpiresAt := lm.leaseExpiresAt
	consecutiveFailures := lm.consecutiveFailures
	gracePeriod := lm.gracePeriod
	lastError := lm.lastError
	stuckThreshold := lm.stuckThresholdSeconds
	expectedHeartbeat := lm.expectedLastHeartbeat
	lm.mu.RUnlock()

	// Build health data map
	healthData := make(map[string]interface{})

	// Add timestamp
	healthData["timestamp"] = time.Now().Format("2006-01-02T15:04:05Z07:00")

	// Add lease info
	healthData["lease_expires_at"] = leaseExpiresAt
	healthData["consecutive_failures"] = consecutiveFailures
	healthData["grace_period"] = gracePeriod.String()
	healthData["stuck_threshold_seconds"] = stuckThreshold
	healthData["expected_last_heartbeat"] = expectedHeartbeat

	if lastError != nil {
		healthData["last_error"] = lastError.Error()
	}

	// Compute fires_in_seconds
	if leaseExpiresAt != "" {
		expiresAt, err := time.Parse(time.RFC3339, leaseExpiresAt)
		if err == nil {
			healthData["fires_in_seconds"] = int(time.Until(expiresAt).Seconds())
		}
	}

	// Marshal and write
	data, err := json.MarshalIndent(healthData, "", "  ")
	if err != nil {
		lm.logger.Error("failed to marshal health data",
			"error", err,
		)
		return
	}

	// Ensure directory exists
	dir := filepath.Dir(lm.healthFilePath)
	if err := os.MkdirAll(dir, 0o755); err != nil {
		lm.logger.Error("failed to create health file directory",
			"error", err,
		)
		return
	}

	if err := os.WriteFile(lm.healthFilePath, data, 0o644); err != nil {
		lm.logger.Error("failed to write health file",
			"error", err,
			"path", lm.healthFilePath,
		)
	}
}

// getClaudeCodeStuckThreshold returns the threshold duration for detecting a
// stuck Claude Code process. Reads CLAUDE_CODE_STUCK_THRESHOLD_SECONDS env var;
// defaults to 600 seconds if unset or invalid.
//
// Binary: new function in b71486df, podmonitor package.
// Default: 0x8BB2C97000 ns = 600,000,000,000 ns = 600s.
// Env var: CLAUDE_CODE_STUCK_THRESHOLD_SECONDS (36 chars).
// Log message on invalid: uses slog.Warn with "value" and "default_seconds"=600.
func getClaudeCodeStuckThreshold() time.Duration {
	const defaultThreshold = 600 * time.Second
	s := os.Getenv("CLAUDE_CODE_STUCK_THRESHOLD_SECONDS")
	if len(s) == 0 {
		return defaultThreshold
	}
	n, err := strconv.Atoi(s)
	if err != nil || n <= 0 {
		slog.Warn("invalid CLAUDE_CODE_STUCK_THRESHOLD_SECONDS, using default",
			"value", s, "default_seconds", 600)
		return defaultThreshold
	}
	return time.Duration(n) * time.Second
}
