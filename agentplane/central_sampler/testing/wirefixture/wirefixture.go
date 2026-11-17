// Package wirefixture encodes provider wire payloads for tests.
package wirefixture

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

// MustMarshalJSON encodes value or fails the test.
func MustMarshalJSON(t testing.TB, value any) []byte {
	t.Helper()
	data, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

// SSEEventStream frames each event as a server-sent-event data line.
func SSEEventStream(events ...string) string {
	var stream strings.Builder
	for _, event := range events {
		fmt.Fprintf(&stream, "data: %s\n\n", event)
	}
	return stream.String()
}
