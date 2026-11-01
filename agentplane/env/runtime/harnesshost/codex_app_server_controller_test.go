package harnesshost

import (
	"context"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/codexappserver"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/harness"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/health/grpc_health_v1"
	"google.golang.org/grpc/status"
)

type executionHealth struct {
	grpc_health_v1.UnimplementedHealthServer
	mu      sync.Mutex
	serving bool
	failure error
}

func (h *executionHealth) Check(context.Context, *grpc_health_v1.HealthCheckRequest) (*grpc_health_v1.HealthCheckResponse, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.failure != nil {
		return nil, h.failure
	}
	state := grpc_health_v1.HealthCheckResponse_NOT_SERVING
	if h.serving {
		state = grpc_health_v1.HealthCheckResponse_SERVING
	}
	return &grpc_health_v1.HealthCheckResponse{Status: state}, nil
}

func (h *executionHealth) set(serving bool, err error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.serving = serving
	h.failure = err
}

func codexHealthRunner(t *testing.T, timeout time.Duration) (*Runner, *executionHealth, string) {
	t.Helper()
	root := t.TempDir()
	socket := filepath.Join(root, "execution.sock")
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	health := &executionHealth{}
	server := grpc.NewServer()
	grpc_health_v1.RegisterHealthServer(server, health)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	marker := filepath.Join(root, "initialized")
	config := lifecycleConfig(t, "", root, nil, "/bin/false")
	config.Codex = &harness.LaunchConfig{Binary: codexAppServerTestBinary(t, marker), BaseURL: "https://codex.example.invalid/v1", Token: "test"}
	config.ExecServerURL = "ws://127.0.0.1:4599"
	config.ExecutionSocket = socket
	config.CodexAppServerReadiness = codexappserver.Readiness{ExecServerDependencyTimeout: timeout}
	return newLifecycleRunner(t, config), health, marker
}

func TestCodexJoinedStartupWaitsForSeparateExecutionHealth(t *testing.T) {
	r, health, marker := codexHealthRunner(t, 5*time.Second)
	health.set(false, status.Error(codes.Unavailable, "starting"))
	type result struct {
		process *codexappserver.Process
		err     error
	}
	results := make(chan result, 2)
	for range 2 {
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
			defer cancel()
			p, err := r.codexAppServer.Host(ctx, nil)
			results <- result{p, err}
		}()
	}
	waitCodexDemand(t, r, 2)
	waitForFile(t, marker)
	waitCodex(t, r, func(v codexappserver.Runtime) bool {
		return v.Phase == codexappserver.Initializing && !v.ExecServerReadyDeadline.IsZero()
	}, "external execution readiness")
	select {
	case got := <-results:
		t.Fatalf("Host returned before execution readiness: %+v", got)
	default:
	}
	health.set(true, nil)
	ready := waitCodex(t, r, func(v codexappserver.Runtime) bool { return v.Phase == codexappserver.Ready && v.Attempt == 2 }, "joined demand ready")

	if !ready.ExecServerReadyDeadline.IsZero() {
		t.Fatal("consumed demand retained deadline")
	}
	for range 2 {
		select {
		case got := <-results:
			if got.err != nil || got.process == nil || got.process.ID() != ready.ProcessID {
				t.Fatalf("joined result=%+v", got)
			}
		case <-time.After(lifecycleTestTimeout):
			t.Fatal("joined waiter did not finish")
		}
	}
}

func TestCodexExecutionRefusalDrainsOverlappedAppServer(t *testing.T) {
	r, health, marker := codexHealthRunner(t, 5*time.Second)
	if _, err := requestCodexAppServer(r); err != nil {
		t.Fatal(err)
	}
	waitForFile(t, marker)
	health.set(false, status.Error(codes.FailedPrecondition, "native executable unavailable"))
	stopped := waitCodex(t, r, func(v codexappserver.Runtime) bool { return v.Phase == codexappserver.Stopped && v.Attempt == 1 }, "execution refusal cleanup")
	if !strings.Contains(stopped.Error, "native executable unavailable") {
		t.Fatalf("lost readiness refusal: %+v", stopped)
	}
	if stopped.ProcessID != "" {
		t.Fatalf("refused app-server retained process: %+v", stopped)
	}
}

func TestCodexExecutorReadinessDeadlineConsumesJoinedDemand(t *testing.T) {
	r, _, _ := codexHealthRunner(t, 2*time.Second)
	if _, err := requestCodexAppServer(r); err != nil {
		t.Fatal(err)
	}
	first := waitCodex(t, r, func(v codexappserver.Runtime) bool {
		return v.Phase == codexappserver.Initializing && !v.ExecServerReadyDeadline.IsZero()
	}, "execution deadline")
	if _, err := requestCodexAppServer(r); err != nil {
		t.Fatal(err)
	}
	joined := codexAppServerRuntime(t, r)
	if joined.ExecServerReadyDeadline != first.ExecServerReadyDeadline {
		t.Fatal("joined demand changed deadline")
	}
	stopped := waitCodex(t, r, func(v codexappserver.Runtime) bool { return v.Phase == codexappserver.Stopped && v.Attempt == 2 }, "execution timeout")
	if !strings.Contains(stopped.Error, "within 2s") || !stopped.ExecServerReadyDeadline.IsZero() {
		t.Fatalf("deadline not consumed: %+v", stopped)
	}
}

func forceStopTestRunner(t *testing.T, r *Runner) {
	t.Helper()
	select {
	case <-r.lifecycle.done.Done():
	default:
		r.cancel()
		<-r.lifecycle.done.Done()
	}
	r.lifecycle.workers.Wait()
	if err := r.owner.Close(); err != nil {
		t.Errorf("close test Runner state owner: %v", err)
	}
}

func codexAppServerRuntime(t *testing.T, r *Runner) codexappserver.Runtime {
	t.Helper()
	current, _, err := codexappserver.ReadRuntime(r.ctx, r.lifecycle.state)
	if err != nil {
		t.Fatal(err)
	}
	return current
}

func waitCodex(t *testing.T, r *Runner, predicate func(codexappserver.Runtime) bool, want string) codexappserver.Runtime {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	for {
		changed := r.changesSince()
		current := codexAppServerRuntime(t, r)
		if predicate(current) {
			return current
		}
		select {
		case <-changed:
		case <-ctx.Done():
			t.Fatalf("timed out waiting for %s: codex=%+v", want, current)
		}
	}
}

func codexAppServerDemand(t *testing.T, r *Runner) uint64 {
	t.Helper()
	input, found, err := codexappserver.ReadInput(r.ctx, r.lifecycle.state)
	if err != nil {
		t.Fatal(err)
	}
	if !found {
		t.Fatal("Codex input is missing")
	}
	return input.Demand
}

func waitCodexDemand(t *testing.T, r *Runner, want uint64) uint64 {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	ticker := time.NewTicker(time.Millisecond)
	defer ticker.Stop()
	for {
		if got := codexAppServerDemand(t, r); got >= want {
			return got
		}
		select {
		case <-ticker.C:
		case <-ctx.Done():
			t.Fatalf("timed out waiting for Codex demand %d", want)
		}
	}
}

func updateLifecycleValueForTest[T lifecycleSpec[T]](t *testing.T, r *Runner, id, owner string, spec T) {
	t.Helper()
	ctx, cancel := context.WithTimeout(r.ctx, lifecycleTestTimeout)
	defer cancel()
	if err := modifyLifecycleForTest(ctx, r, id, owner, func(current *T) { *current = spec }); err != nil {
		t.Fatal(err)
	}
}

func requestCodexAppServer(r *Runner) (uint64, error) {
	var demand uint64
	err := r.admitCodexRequest(r.ctx, func(ctx context.Context) error {
		var err error
		demand, err = r.codexAppServer.RequestLocked(ctx, nil)
		return err
	})
	return demand, err
}

func codexAppServerTestBinary(t *testing.T, initializedMarker string) string {
	t.Helper()
	binary := filepath.Join(t.TempDir(), "codex-app-server")
	marker := shellQuote(initializedMarker)
	script := fmt.Sprintf("#!/bin/sh\ntrap 'exit 0' TERM INT\nIFS= read -r initialize\nprintf '%%s\\n' '{\"id\":\"agentplane-initialize\",\"result\":{}}'\nIFS= read -r initialized\nprintf initialized > %s\ncat >/dev/null\n", marker)
	if err := os.WriteFile(binary, []byte(script), 0o700); err != nil {
		t.Fatal(err)
	}
	return binary
}
