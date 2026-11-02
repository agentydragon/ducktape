package sessioninstructions

import (
	"encoding/json"
	"strings"
)

// SandboxIdentity names the Sandbox bound to one Session.
type SandboxIdentity struct {
	Namespace string `json:"namespace"`
	Name      string `json:"name"`
	UID       string `json:"uid"`
}

// Destination identifies the current Session within its Sandbox.
type Destination struct {
	Sandbox   SandboxIdentity `json:"destination_ref"`
	SessionID string          `json:"session_id"`
}

// DestinationJSON serializes the notification destination consumed by Agentplane's CLI.
func DestinationJSON(destination Destination) string {
	wire, _ := json.Marshal(destination)
	// The serialized value contains only strings, so encoding/json cannot fail.
	return string(wire)
}

// Compose combines the platform guide, Session destination, and caller brief in that order.
func Compose(platformInstructions string, destination Destination, additionalInstructions string) string {
	destinationInstructions := "Your explicit notification destination is:\n" + DestinationJSON(destination) +
		"\nUse these identifiers when addressing this session; they are not credentials."
	parts := make([]string, 0, 3)
	for _, part := range []string{platformInstructions, destinationInstructions, additionalInstructions} {
		if part = strings.TrimSpace(part); part != "" {
			parts = append(parts, part)
		}
	}
	return strings.Join(parts, "\n\n")
}
