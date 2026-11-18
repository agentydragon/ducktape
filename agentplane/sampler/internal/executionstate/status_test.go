package executionstate

import "testing"

func TestDecodeProcessLifecycle(t *testing.T) {
	// These spellings are retained in sampler_process and survive sampler rolls.
	for _, value := range []string{"starting", "running", "exited", "exited_unread", "abandoned", "unknown"} {
		status, err := ParseProcessStatus(value)
		if err != nil || string(status) != value {
			t.Fatalf("decode %q = %q, %v", value, status, err)
		}
	}
	for _, value := range []string{"", "future-fixture-state", "EXITED"} {
		if status, err := ParseProcessStatus(value); err == nil || status != "" {
			t.Fatalf("accepted invalid status %q: %q, %v", value, status, err)
		}
	}
}

func TestDecodeCellLifecycle(t *testing.T) {
	// These spellings are retained in sampler_cell and survive sampler rolls.
	for _, value := range []string{"live", "completed", "terminated", "abandoned"} {
		status, err := ParseCellStatus(value)
		if err != nil || string(status) != value {
			t.Fatalf("decode %q = %q, %v", value, status, err)
		}
	}
	for _, value := range []string{"", "unknown", "future-fixture-state"} {
		if status, err := ParseCellStatus(value); err == nil || status != "" {
			t.Fatalf("accepted invalid status %q: %q, %v", value, status, err)
		}
	}
}
