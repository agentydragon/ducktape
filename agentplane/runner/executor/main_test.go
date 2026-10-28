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

	"github.com/agentydragon/ducktape/agentplane/runner/executor/control"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
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
