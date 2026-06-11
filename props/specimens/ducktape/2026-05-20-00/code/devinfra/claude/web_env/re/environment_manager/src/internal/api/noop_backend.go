// Reconstructed from a6f96673 DWARF extraction, carried forward to 495ea204.
// Source: internal/api/noop_backend.go
//
// Original source path:
//   /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/api/noop_backend.go
//
// Key symbols:
//   - api.(*NoopBackend).PostEvent (0x826420)
//   - api.(*NoopBackend).FlushLogs (0x826440)
//   - api.(*NoopBackend).OtlpEndpoints (0x826460)
//
// itab: *NoopBackend → SessionBackend (0xfb57e0)
//
// No-op backend for setup-only session mode. Implements the SessionBackend
// interface with empty methods that return nil/zero values.

package api

import (
	"context"
)

// NoopBackend is a no-op implementation of SessionBackend used when the
// environment manager runs in setup-only mode (no CCR communication needed).
type NoopBackend struct{}

// PostEvent is a no-op. Returns nil error.
//
// Binary address: 0x826420
// Source line: 16
// Assembly: XORL AX, AX; XORL BX, BX; RET
func (b *NoopBackend) PostEvent(ctx context.Context, event interface{}) error {
	return nil
}

// FlushLogs is a no-op. Returns nil error.
//
// Binary address: 0x826440
// Source line: 20
// Assembly: XORL AX, AX; XORL BX, BX; RET
func (b *NoopBackend) FlushLogs(ctx context.Context, sessionID string, logs []DiagLogEntry) error {
	return nil
}

// OtlpEndpoints is a no-op. Returns empty strings and nil error.
//
// Binary address: 0x826460
// Source lines: 23-24
// Assembly: zeroes out 5 return values (3 empty strings + 2 zeros) via stack, returns
func (b *NoopBackend) OtlpEndpoints(ctx context.Context) (string, string, string, string, error) {
	return "", "", "", "", nil
}
