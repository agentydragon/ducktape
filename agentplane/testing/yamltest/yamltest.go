// Package yamltest provides helpers for testing YAML decoders.
package yamltest

import (
	"bytes"
	"encoding/json"
	"testing"
)

// JSONInput normalizes a JSON document so yaml.v3 sees Unicode code points rather than surrogate pairs.
func JSONInput(t testing.TB, document json.RawMessage) string {
	t.Helper()
	decoder := json.NewDecoder(bytes.NewReader(document))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		t.Fatal(err)
	}
	written, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return string(written)
}
