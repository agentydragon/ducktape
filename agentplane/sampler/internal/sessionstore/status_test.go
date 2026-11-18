package sessionstore

import (
	"testing"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/executionstate"
)

func TestStoredLifecycleConversion(t *testing.T) {
	for _, raw := range []any{"exited_unread", []byte("exited_unread")} {
		status := processStatus(executionstate.ProcessUnknown)
		if err := status.Scan(raw); err != nil || status != processStatus(executionstate.ProcessExitedUnread) {
			t.Fatalf("process scan %v = %q: %v", raw, status, err)
		}
		value, err := status.Value()
		if err != nil || value != "exited_unread" {
			t.Fatalf("process SQL value = %v: %v", value, err)
		}
	}
	for _, raw := range []any{"completed", []byte("completed")} {
		status := cellStatus(executionstate.CellLive)
		if err := status.Scan(raw); err != nil || status != cellStatus(executionstate.CellCompleted) {
			t.Fatalf("cell scan %v = %q: %v", raw, status, err)
		}
		value, err := status.Value()
		if err != nil || value != "completed" {
			t.Fatalf("cell SQL value = %v: %v", value, err)
		}
	}
	for _, raw := range []any{nil, 123, "fixture-future-state", []byte("fixture-future-state")} {
		process := processStatus(executionstate.ProcessUnknown)
		cell := cellStatus(executionstate.CellLive)
		if err := process.Scan(raw); err == nil || process != processStatus(executionstate.ProcessUnknown) {
			t.Fatalf("invalid process scan %v = %q: %v", raw, process, err)
		}
		if err := cell.Scan(raw); err == nil || cell != cellStatus(executionstate.CellLive) {
			t.Fatalf("invalid cell scan %v = %q: %v", raw, cell, err)
		}
	}
	if _, err := processStatus("fixture-future-state").Value(); err == nil {
		t.Fatal("invalid process status reached SQL")
	}
	if _, err := cellStatus("fixture-future-state").Value(); err == nil {
		t.Fatal("invalid cell status reached SQL")
	}
}
