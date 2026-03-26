// Reconstructed from a6f96673 DWARF extraction, carried forward to 64bc4dc1.
// Source: internal/session/noop_activity_recorder.go
//
// Original source path:
//   /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/session/noop_activity_recorder.go
//
// Key symbols:
//   - session.(*NoopActivityRecorder).RecordActivity (0x840d80)
//   - session.(*NoopActivityRecorder).RecordLongRunningActivity (0x840da0)
//   - session.(*NoopActivityRecorder).RecordFailureResult (0x840dc0)
//   - session.(*noopStopper).Stop (0x840d60)
//
// itabs:
//   - *NoopActivityRecorder → ActivityRecorder (0xfb58a0)
//   - *noopStopper → util.Stopper (0xfad8a0)
//
// No-op activity recorder implementation used when session ingress is not
// available. All methods are trivial no-ops returning nil/zero values.
//
// Note: In the 64bc4dc1 RE tree, these types were initially combined into
// activity_recorder.go. This file matches the original DWARF source file
// separation from a6f96673.

package session

import (
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/api"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/util"
)

// NoopActivityRecorder is a no-op implementation of ActivityRecorder.
// Used when session ingress is not available.
type NoopActivityRecorder struct{}

// noopStopper is a no-op implementation of util.Stopper.
// Binary: go:itab.*session.noopStopper,util.Stopper (0xfad8a0)
type noopStopper struct{}

// Stop is a no-op.
//
// Binary address: 0x840d60
// Assembly: RET
func (n *noopStopper) Stop() {}

// RecordActivity is a no-op. Returns nil.
//
// Binary address: 0x840d80
// Source line: 27
// Assembly: RET
func (n *NoopActivityRecorder) RecordActivity(category api.LogCategory, eventType string) error {
	return nil
}

// RecordLongRunningActivity returns nil (no periodic invoker needed for no-op).
//
// Binary address: 0x840da0
// Source line: 33
// Assembly: LEA itab.*noopStopper,util.Stopper → AX; LEA noopStopper → BX; RET
//
// Note: The binary returns a noopStopper as a util.Stopper interface, but
// the ActivityRecorder interface declares *util.PeriodicInvoker return type.
// PeriodicInvoker implements Stopper. Returning nil satisfies the interface.
func (n *NoopActivityRecorder) RecordLongRunningActivity(category api.LogCategory, eventType string) *util.PeriodicInvoker {
	return nil
}

// RecordFailureResult is a no-op. Returns nil.
//
// Binary address: 0x840dc0
// Source line: 37
// Assembly: XORL AX, AX; XORL BX, BX; RET
func (n *NoopActivityRecorder) RecordFailureResult(category api.LogCategory, eventType string, errMsg string) error {
	return nil
}
