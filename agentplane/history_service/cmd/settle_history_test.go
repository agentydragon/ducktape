package main

import (
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/history_service/internal/storage"
)

func TestHistoryMaintenanceRequiresAnExplicitScopeAndWritePolicy(t *testing.T) {
	const session = "00000000-0000-4000-8000-000000000001"
	for _, args := range [][]string{
		nil,
		{"--all"},
		{"--dry-run"},
		{"--all", "--apply", "--dry-run"},
		{"--all", "--session", session, "--apply"},
		{"--session", "bad", "--apply"},
		{"--session", "00000000-0000-0000-0000-000000000000", "--apply"},
		{"--all", "--apply", "extra"},
	} {
		if _, err := parseHistoryMaintenanceOptions("settle-history", args); err == nil {
			t.Fatalf("accepted %v", args)
		}
	}
	for _, apply := range []bool{false, true} {
		mode := "--dry-run"
		if apply {
			mode = "--apply"
		}
		one, err := parseHistoryMaintenanceOptions("compact-history", []string{"--session", session, mode})
		if err != nil || one.all || one.session.String() != session || one.apply != apply || one.bytesPerSecond != 1<<20 ||
			one.batchBudget != storage.DefaultRepackBatchBudget {
			t.Fatalf("one Session: %+v %v", one, err)
		}
		all, err := parseHistoryMaintenanceOptions("compact-history", []string{"--all", mode})
		if err != nil || !all.all || all.apply != apply {
			t.Fatalf("all Sessions: %+v %v", all, err)
		}
	}
	if _, err := parseHistoryMaintenanceOptions("settle-history", []string{"--all", "--apply", "--bytes-per-second", "100"}); err == nil {
		t.Fatal("settle-history accepted an unrelated repack option")
	}
	for _, args := range [][]string{
		{"--all", "--apply", "--lock-wait=0s"},
		{"--all", "--apply", "--lock-wait=100us"},
		{"--all", "--apply", "--lock-wait=61s"},
		{"--all", "--apply", "--guard-check-interval=0s"},
		{"--all", "--apply", "--bytes-per-second", "0"},
		{"--all", "--apply", "--batch-transaction-budget", "0s"},
		{"--all", "--apply", "--max-replica-lag-bytes", "1"},
		{"--all", "--apply", "--min-free-bytes", "1"},
		{"--all", "--apply", "--guard-file", "/tmp/history-guard.json", "--guard-max-age", "0s"},
	} {
		if _, err := parseHistoryMaintenanceOptions("compact-history", args); err == nil {
			t.Fatalf("accepted unsafe repack controls %v", args)
		}
	}
	guarded, err := parseHistoryMaintenanceOptions("compact-history", []string{
		"--all", "--apply", "--guard-file", "/tmp/history-guard.json", "--max-replica-lag-bytes", "4096",
		"--min-free-bytes", "8192", "--pause-file", "/tmp/pause", "--bytes-per-second", "1024",
		"--batch-transaction-budget", "3s", "--guard-check-interval=2s", "--lock-wait=1500ms",
	})
	if err != nil || guarded.maxReplicaLagBytes != 4096 || guarded.minFreeBytes != 8192 ||
		guarded.pauseFile != "/tmp/pause" || guarded.bytesPerSecond != 1024 || guarded.batchBudget != 3*time.Second || guarded.guardInterval != 2*time.Second || guarded.lockWait != 1500*time.Millisecond {
		t.Fatalf("guarded options: %+v %v", guarded, err)
	}
}
