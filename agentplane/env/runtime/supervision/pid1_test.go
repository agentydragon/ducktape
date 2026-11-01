package supervision

import (
	"context"
	"os"
	"strings"
	"testing"
)

func TestPID1BackendRejectsNonPID1(t *testing.T) {
	owner, err := os.CreateTemp(t.TempDir(), "owner")
	if err != nil {
		t.Fatal(err)
	}
	defer owner.Close()
	if _, err := NewPID1Supervisor(owner); err == nil || !strings.Contains(err.Error(), "requires Runner to be PID 1") {
		t.Fatalf("PID1 backend accepted a non-PID1 process: %v", err)
	}
	if _, err := NewPID1SupervisorWithoutStateOwner(); err == nil || !strings.Contains(err.Error(), "requires Runner to be PID 1") {
		t.Fatalf("state-less PID1 backend accepted a non-PID1 process: %v", err)
	}
}

func TestSubprocessSupervisorCannotFenceNamespace(t *testing.T) {
	err := NewSubprocessSupervisor(nil).FenceNamespace(context.Background())
	if err == nil || !strings.Contains(err.Error(), "does not own a private PID namespace") {
		t.Fatalf("subprocess supervisor fenced a PID namespace: %v", err)
	}
}
