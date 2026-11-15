package session

import (
	"testing"
	"time"
)

func TestLoginAttemptMergePreservesConcurrentKeys(t *testing.T) {
	before := map[string]Attempt{"ours": {Nonce: "old"}}
	after := map[string]Attempt{"ours": {Nonce: "new"}}
	current := map[string]Attempt{
		"ours":  {Nonce: "old"},
		"other": {Nonce: "concurrent", ExpiresAt: time.Unix(1, 0)},
	}

	merged := mergeLoginAttempts(current, before, after)
	want := map[string]Attempt{
		"ours":  {Nonce: "new"},
		"other": {Nonce: "concurrent", ExpiresAt: time.Unix(1, 0)},
	}
	if len(merged) != len(want) {
		t.Fatalf("merged = %#v, want %#v", merged, want)
	}
	for key, value := range want {
		if merged[key] != value {
			t.Errorf("merged[%q] = %#v, want %#v", key, merged[key], value)
		}
	}
}
