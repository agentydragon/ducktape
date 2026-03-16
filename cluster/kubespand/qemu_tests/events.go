// Package qemu_tests provides the shared event protocol and test helpers
// for QEMU-based integration tests.
package qemu_tests

// EventType identifies the phase or action being reported by a VM.
type EventType string

const (
	EventProbe EventType = "probe"
	EventError EventType = "error"
	EventDone  EventType = "done"
	// EventReady indicates the VM has started all services (kubespand, TCP
	// listener, probe server) and is ready for test host commands.
	EventReady EventType = "ready"
)

// Event is a structured message emitted by a QEMU VM as a JSON line.
type Event struct {
	Type    EventType `json:"type"`
	Message string    `json:"msg"`
	Target  string    `json:"target,omitempty"`
	Success *bool     `json:"success,omitempty"`
	Error   string    `json:"error,omitempty"`
}
