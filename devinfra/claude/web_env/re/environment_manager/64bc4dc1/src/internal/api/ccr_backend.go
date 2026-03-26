// Reconstructed from a6f96673 DWARF extraction, carried forward to 64bc4dc1.
// Source: internal/api/ccr_backend.go
//
// Original source path:
//   /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/api/ccr_backend.go
//
// Key symbols:
//   - api.(*CCRBackend).RegisterWorker (0x823080)
//   - api.(*CCRBackend).WorkerEpoch (0x823380)
//   - api.(*CCRBackend).PostEvent (0x8233a0)
//   - api.(*CCRBackend).FlushLogs (0x823720)
//   - api.(*CCRBackend).OtlpEndpoints (0x823d60)
//   - api.(*CCRBackend).endpoint (0x823fc0)
//
// itab: *CCRBackend → SessionBackend (0xfb5810)

package api

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/url"
)

// CCRBackend is the CCR v2 backend that implements the SessionBackend interface.
// It communicates with the CCR (Claude Code Runner) v2 API for worker
// registration, event posting, log flushing, and OTLP endpoint retrieval.
//
// Struct layout (from field access patterns in endpoint/RegisterWorker):
//
//	offset 0x00: Client     *HttpClient          (pointer to shared HTTP client)
//	offset 0x08: ApiKey     string               (API key, ptr + len)
//	offset 0x18: SessionID  string               (session identifier, ptr + len)
//	offset 0x28: Logger     *slog.Logger
//	offset 0x30: WorkerEpochValue int64          (worker epoch returned from registration)
type CCRBackend struct {
	Client           *HttpClient  // offset 0x00
	ApiKey           string       // offset 0x08
	SessionID        string       // offset 0x18
	Logger           *slog.Logger // offset 0x28
	WorkerEpochValue int64        // offset 0x30
}

// RegisterWorker registers a worker with the CCR v2 API and stores the returned
// worker epoch value.
//
// Binary address: 0x823080
// Source lines: 40-63
//
// Assembly flow:
//  1. Calls endpoint("register") at line 47
//  2. Creates map with "worker_id" key from ApiKey at line 48
//  3. Calls PostJSONWithResponse at line 45
//  4. On error: fmt.Errorf("register worker failed: %w") at line 53
//  5. On success: unmarshals response to extract worker_epoch
func (b *CCRBackend) RegisterWorker(ctx context.Context) (int64, error) {
	endpoint := b.endpoint("register")

	payload := map[string]interface{}{
		"worker_id": b.ApiKey,
	}

	resp, err := b.Client.PostJSONWithResponse(ctx, endpoint, payload, b.ApiKey)
	if err != nil {
		return 0, fmt.Errorf("register worker failed: %w", err)
	}

	// Parse response to extract worker_epoch
	var result struct {
		WorkerEpoch int64 `json:"worker_epoch,string"`
	}
	if err := json.Unmarshal(resp, &result); err != nil {
		return 0, fmt.Errorf("failed to parse register response: %w", err)
	}

	b.WorkerEpochValue = result.WorkerEpoch
	return result.WorkerEpoch, nil
}

// WorkerEpoch returns the stored worker epoch value.
//
// Binary address: 0x823380
// Source line: 68
// Assembly: MOVQ 0x30(AX), AX; RET
func (b *CCRBackend) WorkerEpoch() int64 {
	return b.WorkerEpochValue
}

// PostEvent marshals an event to JSON and posts it to the CCR v2 events endpoint.
//
// Binary address: 0x8233a0
// Source lines: 72-93
//
// Assembly flow:
//  1. json.Marshal(event) at line 73
//  2. On marshal error: fmt.Errorf("failed to marshal event for CCR: %w") at line 75
//  3. json.Unmarshal into a temporary struct at line 78
//  4. On unmarshal error: wraps error at line 79
//  5. Builds endpoint via endpoint("events") at line 84
//  6. Builds outer payload map: {"events": [{"message": unmarshaled}], "worker_epoch": epoch} at lines 85-87
//  7. Posts via Client.doPostJSON at line 82 (content-type "application/json")
func (b *CCRBackend) PostEvent(ctx context.Context, event interface{}) error {
	eventJSON, err := json.Marshal(event)
	if err != nil {
		return fmt.Errorf("failed to marshal event for CCR: %w", err)
	}

	// Unmarshal back into a generic structure for re-wrapping.
	var eventData interface{}
	if err := json.Unmarshal(eventJSON, &eventData); err != nil {
		return fmt.Errorf("failed to unmarshal event for CCR: %w", err)
	}

	endpoint := b.endpoint("events")

	// Build the outer payload: {"events": [{"message": eventData}], "worker_epoch": epoch}
	innerMap := map[string]interface{}{
		"message": eventData,
	}
	payload := map[string]interface{}{
		"events":       []interface{}{innerMap},
		"worker_epoch": b.WorkerEpochValue,
	}

	return b.Client.doPostJSON(ctx, endpoint, b.ApiKey, payload)
}

// FlushLogs flushes diagnostic logs to the CCR v2 diagnostics endpoint.
//
// Binary address: 0x823720
// Source lines: 95-131
//
// Assembly flow:
//  1. If logs slice is nil (line 96): return nil
//  2. makeslice for wire entries at line 100
//  3. Iterate logs (lines 101-112): for each entry, create a map with
//     "timestamp" (formatted as "2006-01-02T15:04:05.000000000Z07:00"),
//     then copy entry.Fields into the map. If entry.Fields has a "level" key,
//     also set "level" in the outer map. Append each map to the entries slice.
//  4. Builds endpoint via endpoint("diagnostics") at line 117
//  5. Builds payload: {"session_id": sessionID, "logs": entries, "worker_epoch": epoch}
//     at lines 118-121
//  6. Posts via Client.doPostJSON at line 115 (content-type "application/json")
//  7. On error: fmt.Errorf("failed to flush diagnostics to CCR: %w") at line 126
//  8. On success: return nil at line 128
func (b *CCRBackend) FlushLogs(ctx context.Context, sessionID string, logs []DiagLogEntry) error {
	if logs == nil {
		return nil
	}

	// Convert log entries to wire format (lines 100-112).
	entries := make([]map[string]interface{}, 0, len(logs))
	for i := 0; i < len(logs); i++ {
		entry := logs[i]

		wireEntry := make(map[string]interface{})

		// Format timestamp (line 103).
		wireEntry["timestamp"] = entry.Timestamp.Format("2006-01-02T15:04:05.000000000Z07:00")

		// Copy fields from entry (lines 105-110).
		if entry.Fields != nil {
			for k, v := range entry.Fields {
				wireEntry[k] = v
			}
		}

		entries = append(entries, wireEntry)
	}

	endpoint := b.endpoint("diagnostics")

	// Build payload (lines 118-121).
	payload := map[string]interface{}{
		"session_id":   sessionID,
		"logs":         entries,
		"worker_epoch": b.WorkerEpochValue,
	}

	err := b.Client.doPostJSON(ctx, endpoint, b.ApiKey, payload)
	if err != nil {
		return fmt.Errorf("failed to flush diagnostics to CCR: %w", err)
	}

	return nil
}

// OtlpEndpoints constructs OTLP endpoint URLs with authentication headers.
//
// Binary address: 0x823d60
// Source lines: 133-142
//
// Assembly flow:
//  1. Calls endpoint("otlp/metrics") at line 135 (string len 0x0c = 12)
//  2. fmt.Sprintf("%s?worker_epoch=%d", metricsEndpoint, workerEpoch) at line 135
//  3. Calls endpoint("otlp/logs") at line 136 (string len 0x09 = 9)
//  4. fmt.Sprintf("%s?worker_epoch=%d", logsEndpoint, workerEpoch) at line 136
//  5. Builds headers map: {"authorization": "Bearer " + ApiKey} at lines 137-138
//  6. Returns (metricsURL, metricsURLLen, logsURL, logsURLLen, headersMap)
//
// Note: The Go return signature is (string, string, string, string, error) per the
// SessionBackend interface, but the binary constructs 2 endpoint URLs and a headers map.
// The 4 return strings map to: metricsEndpoint, logsEndpoint, and the authorization
// header key/value pair.
func (b *CCRBackend) OtlpEndpoints(ctx context.Context) (string, string, string, string, error) {
	metricsEndpoint := b.endpoint("otlp/metrics")
	metricsURL := fmt.Sprintf("%s?worker_epoch=%d", metricsEndpoint, b.WorkerEpochValue)

	logsEndpoint := b.endpoint("otlp/logs")
	logsURL := fmt.Sprintf("%s?worker_epoch=%d", logsEndpoint, b.WorkerEpochValue)

	bearerToken := "Bearer " + b.ApiKey

	return metricsURL, logsURL, "authorization", bearerToken, nil
}

// endpoint constructs the full URL for a CCR v2 backend endpoint.
// Format: "%s/v2/sessions/%s/%s" using the base URL, URL-escaped session ID,
// and the action path.
//
// Binary address: 0x823fc0
// Source lines: 144-149
//
// Assembly flow:
//  1. url.PathEscape(sessionID) at line 148
//  2. fmt.Sprintf("%s/v2/sessions/%s/%s", baseURL, escapedSessionID, action) at line 145
func (b *CCRBackend) endpoint(action string) string {
	escapedSessionID := url.PathEscape(b.SessionID)
	return fmt.Sprintf("%s/v2/sessions/%s/%s",
		b.Client.BaseURL,
		escapedSessionID,
		action,
	)
}
