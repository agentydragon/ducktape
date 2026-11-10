package executor

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
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

func TestCompanionRefusesUnavailableCodexAndKeepsServing(t *testing.T) {
	dir := t.TempDir()
	socket := filepath.Join(dir, "control.sock")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	supervisor, err := supervision.NewPID1Supervisor()
	if err != nil {
		t.Fatal(err)
	}
	served := make(chan error, 1)
	go func() {
		served <- serve(
			ctx,
			socket,
			filepath.Join(dir, "absent-codex"),
			"ws://127.0.0.1:4501",
			"127.0.0.1:0",
			supervisor,
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

func TestAwaitClaimLeaseEndHandlesBufferedAndSocketWhitespace(t *testing.T) {
	tests := []struct {
		name   string
		tail   string
		socket string
	}{
		{name: "newline buffered by JSON decoder", tail: "\n"},
		{name: "newline still on socket", socket: "\n"},
		{name: "request without newline"},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			client, server := net.Pipe()
			defer client.Close()
			defer server.Close()
			result := make(chan error, 1)
			buffered := decoderBufferedTail(t, test.tail)
			go func() { result <- awaitClaimLeaseEnd(buffered, server) }()
			if test.socket != "" {
				writeDone := make(chan error, 1)
				go func() {
					_, err := client.Write([]byte(test.socket))
					writeDone <- err
				}()
				select {
				case err := <-writeDone:
					if err != nil {
						t.Fatal(err)
					}
				case <-time.After(time.Second):
					t.Fatal("server did not consume socket whitespace")
				}
			}
			select {
			case err := <-result:
				t.Fatalf("lease ended before peer EOF: %v", err)
			case <-time.After(50 * time.Millisecond):
			}
			_ = client.Close()
			select {
			case err := <-result:
				if !errors.Is(err, io.EOF) {
					t.Fatalf("lease close error = %v, want EOF", err)
				}
			case <-time.After(time.Second):
				t.Fatal("lease reader did not return after peer EOF")
			}
		})
	}
}

func TestAwaitClaimLeaseEndRejectsUnexpectedData(t *testing.T) {
	for _, test := range []struct {
		name   string
		tail   string
		socket string
	}{
		{name: "invalid byte buffered by JSON decoder", tail: "x"},
		{name: "invalid byte still on socket", socket: "x"},
	} {
		t.Run(test.name, func(t *testing.T) {
			client, server := net.Pipe()
			defer client.Close()
			defer server.Close()
			result := make(chan error, 1)
			buffered := decoderBufferedTail(t, test.tail)
			go func() { result <- awaitClaimLeaseEnd(buffered, server) }()
			if test.socket != "" {
				writeDone := make(chan error, 1)
				go func() {
					_, err := client.Write([]byte(test.socket))
					writeDone <- err
				}()
				select {
				case err := <-writeDone:
					if err != nil {
						t.Fatal(err)
					}
				case <-time.After(time.Second):
					t.Fatal("server did not read unexpected socket data")
				}
			}
			select {
			case err := <-result:
				if !errors.Is(err, errUnexpectedClaimLeaseData) {
					t.Fatalf("lease data error = %v, want unexpected-data error", err)
				}
			case <-time.After(time.Second):
				t.Fatal("lease reader did not reject unexpected data")
			}
		})
	}
}

func decoderBufferedTail(t *testing.T, tail string) io.Reader {
	t.Helper()
	decoder := json.NewDecoder(strings.NewReader(`{"operation":"claim"}` + tail))
	var request control.Request
	if err := decoder.Decode(&request); err != nil {
		t.Fatalf("decode claim request: %v", err)
	}
	buffered := decoder.Buffered()
	got, err := io.ReadAll(buffered)
	if err != nil {
		t.Fatalf("read decoder buffer: %v", err)
	}
	if string(got) != tail {
		t.Fatalf("decoder buffered %q, want %q", got, tail)
	}
	return decoder.Buffered()
}

func TestClaimLeaseStaysOwnedUntilEOF(t *testing.T) {
	dir := t.TempDir()
	scriptPath := filepath.Join(dir, "exec-server")
	readyPath := filepath.Join(dir, "ready")
	binaryPath := filepath.Join(dir, "codex-wrapper")
	if err := os.WriteFile(scriptPath, []byte(": > \"$READY_FILE\"\nwhile :; do sleep 1; done\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(binaryPath, []byte("#!/bin/sh\nexec /bin/sh \"$EXEC_SERVER_SCRIPT\"\n"), 0o700); err != nil {
		t.Fatal(err)
	}
	socket := filepath.Join(dir, "control.sock")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	supervisor, err := supervision.NewPID1Supervisor()
	if err != nil {
		t.Fatal(err)
	}
	served := make(chan error, 1)
	go func() {
		served <- serve(ctx, socket, binaryPath, "ws://127.0.0.1:4501", "127.0.0.1:0", supervisor)
	}()
	t.Cleanup(func() {
		cancel()
		select {
		case err := <-served:
			if err != nil {
				t.Errorf("companion after cancellation: %v", err)
			}
		case <-time.After(5 * time.Second):
			t.Error("companion did not stop after cancellation")
		}
	})
	waitForSocket(t, socket)

	for _, test := range []struct {
		name       string
		useEncoder bool
		terminator string
	}{
		{name: "JSON without newline"},
		{name: "JSON encoder newline", useEncoder: true, terminator: "\n"},
	} {
		t.Run(test.name, func(t *testing.T) {
			_ = os.Remove(readyPath)
			claimCtx, claimCancel := context.WithTimeout(ctx, 5*time.Second)
			var connection net.Conn
			var status control.Status
			var err error
			request := control.Request{Operation: "claim", Environment: map[string]string{
				"PATH": "/usr/bin:/bin", "EXEC_SERVER_SCRIPT": scriptPath, "READY_FILE": readyPath,
			}}
			if test.useEncoder {
				connection, status, err = control.Connect(claimCtx, socket, request)
			} else {
				connection, status, err = claimWithTerminator(claimCtx, socket, request, test.terminator)
			}
			claimCancel()
			if err != nil {
				t.Fatal(err)
			}
			if !status.Owned {
				_ = connection.Close()
				t.Fatalf("claim status = %+v, want owned", status)
			}
			t.Cleanup(func() { _ = connection.Close() })
			waitForFile(t, readyPath)
			inspectCtx, inspectCancel := context.WithTimeout(ctx, 3*time.Second)
			inspected, err := inspectCompanion(inspectCtx, socket)
			inspectCancel()
			if err != nil {
				t.Fatal(err)
			}
			if !inspected.Owned || inspected.Generation != status.Generation {
				t.Fatalf("claim lease ended before EOF: initial=%+v inspected=%+v", status, inspected)
			}

			_ = connection.Close()
			waitForIdleGeneration(t, ctx, socket, status.Generation)
		})
	}
}

func claimWithTerminator(ctx context.Context, socket string, request control.Request, terminator string) (net.Conn, control.Status, error) {
	connection, err := (&net.Dialer{}).DialContext(ctx, "unix", socket)
	if err != nil {
		return nil, control.Status{}, err
	}
	deadline, _ := ctx.Deadline()
	_ = connection.SetDeadline(deadline)
	data, err := json.Marshal(request)
	if err == nil {
		data = append(data, terminator...)
		for len(data) > 0 {
			var n int
			n, err = connection.Write(data)
			if err != nil {
				break
			}
			data = data[n:]
		}
	}
	if err != nil {
		_ = connection.Close()
		return nil, control.Status{}, err
	}
	var status control.Status
	if err := json.NewDecoder(connection).Decode(&status); err != nil {
		_ = connection.Close()
		return nil, status, err
	}
	_ = connection.SetDeadline(time.Time{})
	return connection, status, nil
}

func inspectCompanion(ctx context.Context, socket string) (control.Status, error) {
	connection, status, err := control.Connect(ctx, socket, control.Request{Operation: "inspect"})
	if connection != nil {
		_ = connection.Close()
	}
	return status, err
}

func waitForIdleGeneration(t *testing.T, ctx context.Context, socket, previous string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		inspectCtx, cancel := context.WithTimeout(ctx, time.Second)
		status, err := inspectCompanion(inspectCtx, socket)
		cancel()
		if err == nil && !status.Owned && status.Generation != previous {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("claim generation %s was not fenced before deadline", previous)
}

func waitForFile(t *testing.T, path string) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(path); err == nil {
			return
		} else if !errors.Is(err, os.ErrNotExist) {
			t.Fatal(err)
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("executor did not create %s", path)
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
