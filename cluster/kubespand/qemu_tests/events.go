// Package qemu_tests provides the shared event protocol and test helpers
// for QEMU-based integration tests.
package qemu_tests

// EventType identifies the phase or action being reported by a VM.
type EventType string

const (
	EventBoot      EventType = "boot"
	EventModules   EventType = "modules"
	EventNetwork   EventType = "network"
	EventKubespand EventType = "kubespand"
	EventDiscovery EventType = "discovery"
	EventProbe     EventType = "probe"
	EventError     EventType = "error"
	EventDone      EventType = "done"
)

// Event is a structured message emitted by a QEMU VM as a JSON line.
type Event struct {
	Type      EventType `json:"type"`
	Timestamp float64   `json:"ts"`
	Role      string    `json:"role"`
	Message   string    `json:"msg"`
	PeerAddr  string    `json:"peer_addr,omitempty"`
	PeerIPv4  string    `json:"peer_ipv4,omitempty"`
	Target    string    `json:"target,omitempty"`
	Success   *bool     `json:"success,omitempty"`
	Error     string    `json:"error,omitempty"`
}
