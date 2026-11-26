package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestRepackControllerPausesOnMissingStaleAndThresholdGuards(t *testing.T) {
	guardPath := filepath.Join(t.TempDir(), "control.json")
	controller := newRepackController(historyMaintenanceOptions{
		guardFile: guardPath, guardMaxAge: time.Minute, maxReplicaLagBytes: 100, minFreeBytes: 1000,
	})
	if reason, err := controller.pauseReason(); err != nil || !strings.Contains(reason, "missing") {
		t.Fatalf("missing guard: %q, %v", reason, err)
	}

	state := repackGuardState{UpdatedAt: time.Now().Add(-time.Hour), ReplicaLagBytes: 10, FreeBytes: 2000}
	writeGuard := func() {
		t.Helper()
		encoded, err := json.Marshal(state)
		if err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(guardPath, encoded, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	writeGuard()
	if reason, err := controller.pauseReason(); err != nil || !strings.Contains(reason, "stale") {
		t.Fatalf("stale guard: %q, %v", reason, err)
	}

	state.UpdatedAt = time.Now().UTC()
	state.ReplicaLagBytes = 101
	writeGuard()
	if reason, err := controller.pauseReason(); err != nil || !strings.Contains(reason, "replica lag") {
		t.Fatalf("replica lag guard: %q, %v", reason, err)
	}

	state.ReplicaLagBytes, state.FreeBytes = 10, 999
	writeGuard()
	if reason, err := controller.pauseReason(); err != nil || !strings.Contains(reason, "free space") {
		t.Fatalf("free-space guard: %q, %v", reason, err)
	}

	state.FreeBytes, state.Pause, state.Reason = 2000, true, "operator request"
	writeGuard()
	if reason, err := controller.pauseReason(); err != nil || !strings.Contains(reason, "operator request") {
		t.Fatalf("operator pause guard: %q, %v", reason, err)
	}

	state.Pause, state.Reason = false, ""
	writeGuard()
	if reason, err := controller.pauseReason(); err != nil || reason != "" {
		t.Fatalf("healthy guard: %q, %v", reason, err)
	}

	pausePath := filepath.Join(t.TempDir(), "pause")
	if err := os.WriteFile(pausePath, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	controller.options.pauseFile = pausePath
	if reason, err := controller.pauseReason(); err != nil || !strings.Contains(reason, pausePath) {
		t.Fatalf("pause file: %q, %v", reason, err)
	}
}
