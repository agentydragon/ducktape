package sessioninstructions

import (
	"strings"
)

// Compose combines the platform guide, Session ID, and caller brief in that order.
func Compose(platformInstructions, sessionID, additionalInstructions string) string {
	sessionInstructions := "Your Session ID is: " + sessionID +
		"\nUse this identifier when addressing this Session; it is not a credential."
	parts := make([]string, 0, 3)
	for _, part := range []string{platformInstructions, sessionInstructions, additionalInstructions} {
		if part = strings.TrimSpace(part); part != "" {
			parts = append(parts, part)
		}
	}
	return strings.Join(parts, "\n\n")
}
