package executor

import (
	"context"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/companion/control"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
)

func TestMain(m *testing.M) {
	if len(os.Args) > 1 && os.Args[1] == supervision.SupervisorMode {
		os.Exit(supervision.SupervisorMain(os.Args[2:]))
	}
	os.Exit(m.Run())
}

func TestCompanionRefusesUnavailableCodexAndKeepsServing(t *testing.T) {
	dir := t.TempDir()
	socket := filepath.Join(dir, "control.sock")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	served := make(chan error, 1)
	go func() {
		served <- serve(
			ctx,
			socket,
			filepath.Join(dir, "absent-codex"),
			"ws://127.0.0.1:4501",
			"127.0.0.1:0",
			supervision.NewSubprocessSupervisor(nil),
		)
	}()
	waitForSocket(t, socket)
	health := httptest.NewServer(healthHandler(socket))
	defer health.Close()
	checkHealth := func() {
		t.Helper()
		response, err := health.Client().Get(health.URL + "/healthz")
		if err != nil {
			t.Fatal(err)
		}
		defer response.Body.Close()
		if response.StatusCode != http.StatusOK {
			t.Fatalf("unclaimed companion health = %d, want 200", response.StatusCode)
		}
	}
	checkHealth()

	for attempt := 1; attempt <= 2; attempt++ {
		connection, status, err := control.Connect(ctx, socket, control.Request{
			Operation:   "claim",
			Environment: map[string]string{"PATH": "/usr/bin"},
		})
		if err != nil {
			t.Fatalf("claim %d: %v", attempt, err)
		}
		_ = connection.Close()
		if status.Owned || !strings.Contains(status.Error, "absent-codex") {
			t.Errorf("claim %d returned %+v, want a refusal with its reason", attempt, status)
		}
		checkHealth()
	}

	connection, status, err := control.Connect(ctx, socket, control.Request{Operation: "inspect"})
	if err != nil {
		t.Fatalf("inspect after refusals: %v", err)
	}
	_ = connection.Close()
	if status.Owned || status.Error != "" {
		t.Errorf("inspect after refusals = %+v, want an idle generation", status)
	}
	cancel()
	if err := <-served; err != nil {
		t.Fatalf("companion after cancellation: %v", err)
	}
}

func TestFenceRequestsStayWithTheirOwnerGeneration(t *testing.T) {
	state := &companionOwnership{
		status:        control.Status{Generation: "g1", Owned: true},
		fenceRequests: make(chan struct{}, 1),
	}
	state.requestFence("g1")
	state.requestFence("g1") // Duplicate lease EOF and explicit fence requests coalesce.
	if len(state.fenceRequests) != 1 || state.pendingFence != "g1" {
		t.Fatalf("duplicate g1 fence requests: pending=%q wakes=%d", state.pendingFence, len(state.fenceRequests))
	}
	<-state.fenceRequests
	state.mu.Lock()
	state.pendingFence = ""
	state.fencing = true
	state.mu.Unlock()
	state.requestFence("g1") // A request during the in-progress fence cannot remain queued.
	if len(state.fenceRequests) != 0 {
		t.Fatal("duplicate g1 fence remained queued during its fence")
	}
	state.mu.Lock()
	state.status = control.Status{Generation: "g2", Owned: true}
	state.fencing = false
	state.mu.Unlock()
	state.requestFence("g1") // Delayed inspect response or old lease EOF.
	if len(state.fenceRequests) != 0 {
		t.Fatal("stale g1 fence request targeted the new owner")
	}
	state.requestFence("g2")
	if len(state.fenceRequests) != 1 || state.pendingFence != "g2" {
		t.Fatalf("g2 fence lost behind stale g1: pending=%q wakes=%d", state.pendingFence, len(state.fenceRequests))
	}
}

func waitForSocket(t *testing.T, path string) {
	t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for {
		if _, err := os.Stat(path); err == nil {
			return
		}
		if time.Now().After(deadline) {
			t.Fatalf("no companion socket at %s", path)
		}
		time.Sleep(10 * time.Millisecond)
	}
}
