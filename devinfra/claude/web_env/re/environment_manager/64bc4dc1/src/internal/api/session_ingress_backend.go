// Reconstructed from a6f96673 DWARF extraction, carried forward to 64bc4dc1.
// Source: internal/api/session_ingress_backend.go
//
// Original source path:
//   /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/api/session_ingress_backend.go
//
// Key symbols:
//   - api.NewSessionIngressBackend (0x827020)
//   - api.(*SessionIngressBackend).PostEvent (0x8271a0)
//   - api.(*SessionIngressBackend).FlushLogs (0x827220)
//   - api.(*SessionIngressBackend).OtlpEndpoints (0x827300)
//
// itab: *SessionIngressBackend → SessionBackend (0xfb5840)
//
// Session ingress backend that delegates to HttpSessionIngressClient for
// posting events and diagnostic logs via the session ingress API.

package api

import (
	"context"
	"fmt"
)

// SessionIngressBackend implements SessionBackend by delegating to
// an HttpSessionIngressClient for session ingress API communication.
//
// Struct layout (from NewSessionIngressBackend at 0x827020, lines 28-32):
//
//	offset 0x00: Client          *HttpSessionIngressClient
//	offset 0x08: SessionID       string  (ptr + len)
//	offset 0x18: APIBaseURL      string  (ptr + len)
//	offset 0x28: ApiKey          string  (ptr + len)
type SessionIngressBackend struct {
	Client     *HttpSessionIngressClient // offset 0x00
	SessionID  string                    // offset 0x08
	APIBaseURL string                    // offset 0x18
	ApiKey     string                    // offset 0x28
}

// NewSessionIngressBackend creates a new SessionIngressBackend with an
// HttpSessionIngressClient configured from the given parameters.
//
// Binary address: 0x827020
// Source lines: 20-32
//
// Assembly flow:
//  1. Calls NewHttpClient(apiBaseURL, ...) at line 26
//  2. Creates HttpSessionIngressClient with client, apiKey, logger, useV2=false at lines 27-32
//  3. Creates SessionIngressBackend with client, sessionID, apiBaseURL, apiKey at lines 28-32
func NewSessionIngressBackend(apiBaseURL string, apiKey string, sessionID string, workerID string, logger interface{}, apiVersion string, useV2 bool) *SessionIngressBackend {
	httpClient := NewHttpClient(apiBaseURL, nil)

	ingressClient := &HttpSessionIngressClient{
		Client: httpClient,
		ApiKey: apiKey,
		Logger: nil,
		UseV2:  false,
	}

	return &SessionIngressBackend{
		Client:     ingressClient,
		SessionID:  sessionID,
		APIBaseURL: apiBaseURL,
		ApiKey:     apiKey,
	}
}

// PostEvent posts a session event via the session ingress client.
//
// Binary address: 0x8271a0
// Source lines: 36-37
//
// Assembly flow:
//  1. Loads Client (offset 0x00), SessionID (offset 0x08+0x10) from receiver
//  2. Passes ctx, SessionID, and event directly to PostSessionIngressEvent
//  3. Returns the result
func (b *SessionIngressBackend) PostEvent(ctx context.Context, event interface{}) error {
	return b.Client.PostSessionIngressEvent(ctx, b.SessionID, event.(*SessionIngressEvent))
}

// FlushLogs flushes diagnostic logs via the session ingress client.
//
// Binary address: 0x827220
// Source lines: 40-44
//
// Assembly flow:
//  1. Loads Client (offset 0x00), SessionID (offset 0x08+0x10) from receiver
//  2. Calls HttpSessionIngressClient.PostForwardDiagLogs(ctx, b.SessionID, logs)
//  3. On error: fmt.Errorf("failed to flush diagnostics to session-ingress: %w") at line 42
//  4. On success: return nil at line 44
func (b *SessionIngressBackend) FlushLogs(ctx context.Context, sessionID string, logs []DiagLogEntry) error {
	err := b.Client.PostForwardDiagLogs(ctx, b.SessionID, logs)
	if err != nil {
		return fmt.Errorf("failed to flush diagnostics to session-ingress: %w", err)
	}
	return nil
}

// OtlpEndpoints constructs OTLP endpoint URLs from the API base URL
// and returns them with an authorization header.
//
// Binary address: 0x827300
// Source lines: 47-54
//
// Assembly flow:
//  1. fmt.Sprintf to build metrics endpoint URL from APIBaseURL at line 50
//  2. fmt.Sprintf to build logs endpoint URL from APIBaseURL at line 51
//  3. makemap with "authorization" (0x0d=13) and session_id (0x0c=12) keys at line 52
//  4. concatstring2: "Bearer " + ApiKey for authorization value at line 53
//  5. SessionID loaded for session_id value at line 54
//  6. Returns (metricsURL, logsURL, headers map) - mapped to 4 strings for interface
func (b *SessionIngressBackend) OtlpEndpoints(ctx context.Context) (string, string, string, string, error) {
	metricsURL := fmt.Sprintf("%s/otlp/v1/metrics", b.APIBaseURL)
	logsURL := fmt.Sprintf("%s/otlp/v1/logs", b.APIBaseURL)

	bearerToken := "Bearer " + b.ApiKey

	return metricsURL, logsURL, "authorization", bearerToken, nil
}
