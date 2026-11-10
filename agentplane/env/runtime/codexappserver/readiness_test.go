package codexappserver

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type executionHealth struct {
	mu       sync.Mutex
	serving  bool
	failure  error
	host     *fixture
	t        *testing.T
	revision uint64
}

// The fake publishes a status change like Runtime's execution controller.
// The app-server observes its metadata and verifies the live owner separately.
type executionHealthEvent struct{ Revision uint64 }

func (executionHealthEvent) ResourceType() resource.Type {
	return lifecycle.ExecutionStatusType
}
func (v executionHealthEvent) DeepCopy() executionHealthEvent { return v }

func (h *executionHealth) executionReady(context.Context) (bool, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.failure != nil {
		if status.Code(h.failure) == codes.Unavailable {
			return false, nil
		}
		return false, h.failure
	}
	return h.serving, nil
}

func (h *executionHealth) set(serving bool, err error) {
	h.mu.Lock()
	h.serving = serving
	h.failure = err
	h.revision++
	revision := h.revision
	h.mu.Unlock()
	if err := cosi.Publish(h.host.ctx, h.host.store, typed.NewResource[executionHealthEvent, lifecycle.Extension[executionHealthEvent]](resource.NewMetadata(lifecycle.Namespace, lifecycle.ExecutionStatusType, lifecycle.ExecutionResourceID, resource.VersionUndefined), executionHealthEvent{Revision: revision}), executionHealthEvent{Revision: revision}, "test-execution"); err != nil {
		h.t.Fatalf("publish execution status: %v", err)
	}
}

const lifecycleTestTimeout = 10 * time.Second

func codexHealthRunner(t *testing.T, timeout time.Duration) (*fixture, *executionHealth, string) {
	t.Helper()
	marker := filepath.Join(t.TempDir(), "initialized")
	health := &executionHealth{t: t}
	f := newFixtureWithConfiguration(t, nil, func(host *Host) {
		host.launch.Binary = codexAppServerTestBinary(t, marker)
		host.deps.ExecServerURL = "ws://127.0.0.1:4599"
		host.deps.ExecutionReady = health.executionReady
		host.deps.Readiness.ExecServerDependencyTimeout = timeout
	})
	health.host = f
	return f, health, marker
}

func TestCodexJoinedStartupWaitsForSeparateExecutionHealth(t *testing.T) {
	// The dependency deadline exceeds the test timeout: readiness must wake on
	// the Execution status event, rather than on the deadline timer.
	r, health, marker := codexHealthRunner(t, 30*time.Second)
	health.set(false, status.Error(codes.Unavailable, "starting"))
	type result struct {
		process *Process
		err     error
	}
	results := make(chan result, 2)
	for range 2 {
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
			defer cancel()
			p, err := r.host.Host(ctx, nil)
			results <- result{p, err}
		}()
	}
	waitCodexDemand(t, r, 2)
	waitForFile(t, marker)
	waitCodexStatus(t, r, func(v Status) bool {
		return v.Phase == Initializing && !v.ExecServerReadyDeadline.IsZero()
	}, "external execution readiness")
	select {
	case got := <-results:
		t.Fatalf("Host returned before execution readiness: %+v", got)
	default:
	}
	health.set(true, nil)
	ready := waitCodexStatus(t, r, func(v Status) bool { return v.Phase == Ready && v.ObservedDemand == 2 }, "joined demand ready")

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
	r, health, marker := codexHealthRunner(t, 30*time.Second)
	if _, err := requestCodexAppServer(r); err != nil {
		t.Fatal(err)
	}
	waitForFile(t, marker)
	health.set(false, status.Error(codes.FailedPrecondition, "native executable unavailable"))
	stopped := waitCodexStatus(t, r, func(v Status) bool { return v.Phase == Stopped && v.ObservedDemand == 1 }, "execution refusal cleanup")
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
	first := waitCodexStatus(t, r, func(v Status) bool {
		return v.Phase == Initializing && !v.ExecServerReadyDeadline.IsZero()
	}, "execution deadline")
	if _, err := requestCodexAppServer(r); err != nil {
		t.Fatal(err)
	}
	joined := codexAppServerStatus(t, r)
	if joined.ExecServerReadyDeadline != first.ExecServerReadyDeadline {
		t.Fatal("joined demand changed deadline")
	}
	stopped := waitCodexStatus(t, r, func(v Status) bool { return v.Phase == Stopped && v.ObservedDemand == 2 }, "execution timeout")
	if !strings.Contains(stopped.Error, "within 2s") || !stopped.ExecServerReadyDeadline.IsZero() {
		t.Fatalf("deadline not consumed: %+v", stopped)
	}
}

func codexAppServerStatus(t *testing.T, r *fixture) Status {
	t.Helper()
	current, _, err := ReadStatus(r.ctx, r.store)
	if err != nil {
		t.Fatal(err)
	}
	return current
}

func waitCodexStatus(t *testing.T, r *fixture, predicate func(Status) bool, want string) Status {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	for {
		r.host.changeMu.Lock()
		changed := r.host.changed
		r.host.changeMu.Unlock()
		current := codexAppServerStatus(t, r)
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

func codexAppServerDemand(t *testing.T, r *fixture) uint64 {
	t.Helper()
	input, found, err := ReadLifecycleSignal(r.ctx, r.store)
	if err != nil {
		t.Fatal(err)
	}
	if !found {
		t.Fatal("Codex app-server lifecycle signal is missing")
	}
	return input.Demand
}

func waitCodexDemand(t *testing.T, r *fixture, want uint64) uint64 {
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

func requestCodexAppServer(r *fixture) (uint64, error) {
	var demand uint64
	err := r.host.deps.RequestAdmission(r.ctx, func(ctx context.Context) error {
		var err error
		demand, err = r.host.RequestLocked(ctx, nil)
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

func shellQuote(value string) string { return "'" + strings.ReplaceAll(value, "'", "'\"'\"'") + "'" }

func waitForFile(t *testing.T, path string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	defer cancel()
	ticker := time.NewTicker(time.Millisecond)
	defer ticker.Stop()
	for {
		if _, err := os.Stat(path); err == nil {
			return
		}
		select {
		case <-ticker.C:
		case <-ctx.Done():
			t.Fatalf("missing marker %s", path)
		}
	}
}
