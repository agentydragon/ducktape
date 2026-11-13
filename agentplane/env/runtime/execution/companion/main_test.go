package executor

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/companion/control"
	controlpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/companion/control/controlpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"google.golang.org/grpc/codes"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
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
	info, err := os.Stat(socket)
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatalf("ownership socket mode = %v, %v; want 0600", info, err)
	}
	connection, err := control.Dial(socket)
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	client := controlpb.NewCompanionControlClient(connection)
	waitForControl(t, client)
	healthStatus, err := healthpb.NewHealthClient(connection).Check(ctx, &healthpb.HealthCheckRequest{
		Service: controlpb.CompanionControl_ServiceDesc.ServiceName,
	})
	if err != nil || healthStatus.GetStatus() != healthpb.HealthCheckResponse_SERVING {
		t.Fatalf("gRPC health = %+v, %v", healthStatus, err)
	}
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
		stream, err := client.Claim(ctx, &controlpb.ClaimRequest{
			Environment: map[string]string{"PATH": "/usr/bin"},
		})
		if err != nil {
			t.Fatalf("claim %d: %v", attempt, err)
		}
		_, err = stream.Recv()
		if status.Code(err) != codes.FailedPrecondition || !strings.Contains(err.Error(), "absent-codex") {
			t.Errorf("claim %d returned %v, want a refusal with its reason", attempt, err)
		}
		checkHealth()
	}

	current, err := client.Inspect(ctx, &controlpb.InspectRequest{})
	if err != nil {
		t.Fatalf("inspect after refusals: %v", err)
	}
	if current.GetOwned() || current.GetGeneration() == "" {
		t.Errorf("inspect after refusals = %+v, want an idle generation", current)
	}
	cancel()
	if err := <-served; err != nil {
		t.Fatalf("companion after cancellation: %v", err)
	}
}

func TestFenceRequestsStayWithTheirOwnerGeneration(t *testing.T) {
	state := &companionOwnership{
		status:        companionStatus{Generation: "g1", Owned: true},
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
	state.status = companionStatus{Generation: "g2", Owned: true}
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

func TestClaimStreamOwnsExecutorUntilCanceledOrFenced(t *testing.T) {
	dir := t.TempDir()
	socket := filepath.Join(dir, "control.sock")
	binary := filepath.Join(dir, "fake-codex")
	ready := filepath.Join(dir, "ready")
	exit := filepath.Join(dir, "exit")
	script := "#!/bin/sh\n: > \"$READY_FILE\"\nwhile [ ! -e \"$EXIT_FILE\" ]; do sleep 0.05; done\n"
	if err := os.WriteFile(binary, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	supervisor, err := supervision.NewPID1Supervisor()
	if err != nil {
		t.Fatal(err)
	}
	served := make(chan error, 1)
	go func() { served <- serve(ctx, socket, binary, "ws://127.0.0.1:4501", "127.0.0.1:0", supervisor) }()
	t.Cleanup(func() {
		cancel()
		select {
		case err := <-served:
			if err != nil {
				t.Errorf("companion after cancellation: %v", err)
			}
		case <-time.After(5 * time.Second):
			t.Error("companion did not stop")
		}
	})
	waitForSocket(t, socket)
	inspectConn, err := control.Dial(socket)
	if err != nil {
		t.Fatal(err)
	}
	defer inspectConn.Close()
	inspector := controlpb.NewCompanionControlClient(inspectConn)
	waitForControl(t, inspector)
	if _, err := inspector.Fence(ctx, &controlpb.FenceRequest{}); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("empty fence generation returned %v, want InvalidArgument", err)
	}
	previous := ""
	for _, ending := range []string{"cancel stream", "close transport", "explicit fence", "executor exit"} {
		t.Run(ending, func(t *testing.T) {
			_ = os.Remove(ready)
			_ = os.Remove(exit)
			connection, err := control.Dial(socket)
			if err != nil {
				t.Fatal(err)
			}
			defer connection.Close()
			leaseCtx, release := context.WithCancel(ctx)
			defer release()
			leaseClient := controlpb.NewCompanionControlClient(connection)
			stream, err := leaseClient.Claim(leaseCtx, &controlpb.ClaimRequest{
				Environment: map[string]string{"PATH": "/usr/bin:/bin", "READY_FILE": ready, "EXIT_FILE": exit},
			})
			if err != nil {
				t.Fatal(err)
			}
			claim, err := stream.Recv()
			if err != nil || claim.GetGeneration() == "" {
				t.Fatalf("claim = %+v, %v", claim, err)
			}
			for deadline := time.Now().Add(5 * time.Second); ; {
				if _, err := os.Stat(ready); err == nil {
					break
				}
				if time.Now().After(deadline) {
					t.Fatal("executor did not start")
				}
				time.Sleep(10 * time.Millisecond)
			}
			// A stale fence must not target this successor.
			if previous != "" {
				if _, err := inspector.Fence(ctx, &controlpb.FenceRequest{Generation: previous}); err != nil {
					t.Fatal(err)
				}
			}
			current, err := inspector.Inspect(ctx, &controlpb.InspectRequest{})
			if err != nil || !current.GetOwned() || current.GetGeneration() != claim.GetGeneration() {
				t.Fatalf("active claim = %+v, %v; want %s", current, err, claim.GetGeneration())
			}
			switch ending {
			case "cancel stream":
				release()
				// Canceling one RPC must leave other RPCs on the same gRPC channel usable.
				waitForIdle(t, leaseClient, claim.GetGeneration())
			case "close transport":
				_ = connection.Close()
			case "explicit fence":
				if _, err := inspector.Fence(ctx, &controlpb.FenceRequest{Generation: claim.GetGeneration()}); err != nil {
					t.Fatal(err)
				}
				if _, err := stream.Recv(); err != io.EOF {
					t.Fatalf("fenced claim stream = %v, want EOF", err)
				}
			case "executor exit":
				if err := os.WriteFile(exit, []byte("exit"), 0o600); err != nil {
					t.Fatal(err)
				}
				if _, err := stream.Recv(); err != io.EOF {
					t.Fatalf("exited executor stream = %v, want EOF", err)
				}
			}
			waitForIdle(t, inspector, claim.GetGeneration())
			previous = claim.GetGeneration()
		})
	}
}

func waitForControl(t *testing.T, client controlpb.CompanionControlClient) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	for {
		if response, err := client.Inspect(ctx, &controlpb.InspectRequest{}); err == nil && response.GetGeneration() != "" {
			return
		}
		if ctx.Err() != nil {
			t.Fatalf("companion gRPC service did not become ready: %v", ctx.Err())
		}
		time.Sleep(10 * time.Millisecond)
	}
}

func waitForIdle(t *testing.T, client controlpb.CompanionControlClient, previous string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	for {
		current, err := client.Inspect(ctx, &controlpb.InspectRequest{})
		if err == nil && !current.GetOwned() && current.GetGeneration() != "" && current.GetGeneration() != previous {
			return
		}
		if ctx.Err() != nil {
			t.Fatalf("generation %s was not fenced: %v", previous, ctx.Err())
		}
		time.Sleep(10 * time.Millisecond)
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
