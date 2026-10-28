package runner

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"testing/synctest"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/coder/websocket"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
)

const codexExecServerTestTimeout = 10 * time.Second

func TestExecutorProbeRequiresValidRPCAndBoundsUnresponsiveServer(t *testing.T) {
	for _, test := range []struct {
		name, response string
		success        bool
	}{
		{"healthy", `{"id":1,"result":{"sessionId":"test-session"}}`, true},
		{"RPC error", `{"id":1,"error":{"code":-32603,"message":"unavailable"}}`, false},
		{"wrong request", `{"id":2,"result":{"sessionId":"test-session"}}`, false},
		{"stalled dispatcher", "", false},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				c, err := websocket.Accept(w, r, nil)
				if err != nil {
					return
				}
				defer c.CloseNow()
				ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
				defer cancel()
				if _, _, err := c.Read(ctx); err != nil {
					return
				}
				if test.response != "" {
					_ = c.Write(ctx, websocket.MessageText, []byte(test.response))
				}
				if test.success {
					if _, _, err := c.Read(ctx); err != nil {
						return
					}
					if _, _, err := c.Read(ctx); err != nil {
						return
					}
					_ = c.Write(ctx, websocket.MessageText, []byte(`{"id":2,"result":{"status":"ready"}}`))
				}
				_, _, _ = c.Read(ctx)
			}))
			defer server.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
			defer cancel()
			err := probeCodexExecServer(ctx, "ws"+strings.TrimPrefix(server.URL, "http"))
			if (err == nil) != test.success {
				t.Fatalf("probe = %v, success=%t", err, test.success)
			}
		})
	}
}

func TestExecutorConfigurationRejectsPartialOrOverlappingIsolation(t *testing.T) {
	for _, endpoint := range []string{"", "ws://0.0.0.0:4501", "ws://127.0.0.1:0", "wss://127.0.0.1:4501", "ws://127.0.0.1:4501/path", "ws://user@127.0.0.1:4501"} {
		if err := validateExecutorURL(endpoint); err == nil {
			t.Fatalf("accepted %q", endpoint)
		}
	}
	const endpoint = "ws://127.0.0.1:4501"
	if err := (CodexExecServerConfig{URL: endpoint, ContainerSocket: "/run/test-executor.sock"}).validate(nil); err != nil {
		t.Fatal(err)
	}
	limits := ResourceLimits{CPUQuotaMicros: 100000, MemoryMaxBytes: 64 * 1024 * 1024, PidsMax: 32, ReadBPS: 1, WriteBPS: 1, ReadIOPS: 1, WriteIOPS: 1}
	harnessGroups := &AgentCgroups{Root: "/test-cgroups/workloads", AggregateLimits: limits, ProcessLimits: limits}
	codexExecServerGroups := *harnessGroups
	codexExecServerGroups.Root = "/test-cgroups/executors"
	if err := (CodexExecServerConfig{URL: endpoint, Isolation: &codexExecServerGroups}).validate(harnessGroups); err != nil {
		t.Fatal(err)
	}
	invalid := codexExecServerGroups
	invalid.ProcessLimits.MemoryMaxBytes++
	for _, config := range []CodexExecServerConfig{
		{URL: endpoint},
		{URL: endpoint, ContainerSocket: "relative"},
		{URL: endpoint, ContainerSocket: "/run/test.sock", Isolation: harnessGroups},
		{URL: endpoint, Isolation: harnessGroups},
		{URL: endpoint, Isolation: &AgentCgroups{Root: "/test-cgroups/workloads/executor"}},
		{URL: endpoint, Isolation: &invalid},
	} {
		if err := config.validate(harnessGroups); err == nil {
			t.Fatalf("accepted %+v", config)
		}
	}
}

func TestExecutorEnsureReturnsTypedCompanionRefusal(t *testing.T) {
	r := newExecutorLifecycleRunner(t, func(context.Context) (*codexExecServerInstance, error) {
		return nil, codexExecServerRefusal{reason: "Codex binary is unavailable"}
	}, nil)
	ctx, cancel := context.WithTimeout(context.Background(), codexExecServerTestTimeout)
	defer cancel()
	err := r.codexExecServer.ensure(ctx)
	var refusal codexExecServerRefusal
	if !errors.As(err, &refusal) || refusal.reason != "Codex binary is unavailable" {
		t.Fatalf("ensure = %v (%T), want the typed companion refusal", err, err)
	}
}

func TestExecutorConcurrentStartupPublishesOneProcessAndClosesThroughCOSI(t *testing.T) {
	entered := make(chan struct{}, 2)
	release := make(chan struct{})
	var starts, stops atomic.Int32
	r := newExecutorLifecycleRunner(t, func(ctx context.Context) (*codexExecServerInstance, error) {
		starts.Add(1)
		entered <- struct{}{}
		select {
		case <-release:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
		return newTestExecutorInstance(&stops), nil
	}, func(context.Context) error { return nil })

	results := make(chan error, 2)
	for range 2 {
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), codexExecServerTestTimeout)
			defer cancel()
			results <- r.codexExecServer.ensure(ctx)
		}()
	}
	select {
	case <-entered:
	case <-time.After(codexExecServerTestTimeout):
		t.Fatal("executor start operation did not begin")
	}
	close(release)
	for range 2 {
		select {
		case err := <-results:
			if err != nil {
				t.Fatalf("concurrent ensure: %v", err)
			}
		case <-time.After(codexExecServerTestTimeout):
			t.Fatal("concurrent ensure did not observe readiness")
		}
	}
	if got := starts.Load(); got != 1 {
		t.Fatalf("started %d executor instances, want one", got)
	}
	if err := r.Close(); err != nil {
		t.Fatalf("close through COSI: %v", err)
	}
	if got := stops.Load(); got != 1 {
		t.Fatalf("fenced %d executor instances, want one", got)
	}
	if current := r.codexExecServer.runtimeState(); current.Phase != codexExecServerStopped || current.StopError != "" {
		t.Fatalf("executor runtime after close = %+v, want a clean stopped state", current)
	}
}

func TestExecutorHealthToleratesFailuresResetsAndFencesBeforeSuccessor(t *testing.T) {
	var starts, stops atomic.Int32
	var probes atomic.Int32
	r := newExecutorLifecycleRunner(t, func(context.Context) (*codexExecServerInstance, error) {
		starts.Add(1)
		return newTestExecutorInstance(&stops), nil
	}, func(context.Context) error {
		switch probes.Add(1) {
		case 1, 4, 8:
			return nil
		case 2, 3, 5, 6, 7:
			return errors.New("temporary dispatcher timeout")
		default:
			return nil
		}
	})

	if err := r.codexExecServer.request(); err != nil {
		t.Fatal(err)
	}
	waitExecutor(t, r, func(current codexExecServerRuntimeSpec) bool { return current.Phase == codexExecServerReady }, "initial readiness")
	for index, failures := range []uint8{1, 2, 0} {
		forceExecutorProbe(t, r)
		waitExecutor(t, r, func(current codexExecServerRuntimeSpec) bool {
			return current.Phase == codexExecServerReady && current.ConsecutiveProbeFailures == failures && probes.Load() >= int32(index+2)
		}, fmt.Sprintf("health failure count %d", failures))
	}

	for _, failures := range []uint8{1, 2, 3} {
		forceExecutorProbe(t, r)
		if failures < 3 {
			waitExecutor(t, r, func(current codexExecServerRuntimeSpec) bool {
				return current.Phase == codexExecServerReady && current.ConsecutiveProbeFailures == failures
			}, fmt.Sprintf("persistent health failure count %d", failures))
		} else {
			waitExecutor(t, r, func(current codexExecServerRuntimeSpec) bool {
				return current.Phase == codexExecServerRetrying && stops.Load() == 1
			}, "third health failure to fence before retry")
		}
	}
	if got := starts.Load(); got != 1 {
		t.Fatalf("started successor before restart delay and fence completion: starts=%d stops=%d", got, stops.Load())
	}
	forceExecutorRetry(t, r)
	waitExecutor(t, r, func(current codexExecServerRuntimeSpec) bool {
		return current.Generation == 2 && current.Phase == codexExecServerReady && starts.Load() == 2 && stops.Load() == 1
	}, "successor after completed fence")
}

func TestExecutorFailedFenceRetainsOwnershipAndBlocksSuccessor(t *testing.T) {
	stateDir := t.TempDir()
	synctest.Test(t, func(t *testing.T) {
		var starts, stops atomic.Int32
		fenceFailure := errors.New("executor container could not prove fencing")
		done := make(chan error, 1)
		instance := &codexExecServerInstance{
			done: done,
			stop: func(context.Context) error {
				stops.Add(1)
				return fenceFailure
			},
		}
		r := newCodexSynctestRunner(t, stateDir, func(context.Context) (*codexExecServerInstance, error) {
			if starts.Add(1) == 1 {
				return instance, nil
			}
			return newTestExecutorInstance(&stops), nil
		})

		if err := r.codexExecServer.request(); err != nil {
			t.Fatal(err)
		}
		synctest.Wait()
		ready := r.codexExecServer.runtimeState()
		if ready.Phase != codexExecServerReady {
			t.Fatalf("executor before native exit = %+v, want ready", ready)
		}
		done <- errors.New("executor exited unexpectedly")
		synctest.Wait()

		failed := r.codexExecServer.runtimeState()
		if !strings.Contains(failed.StopError, fenceFailure.Error()) {
			t.Fatalf("executor StopError = %q, want %q", failed.StopError, fenceFailure)
		}
		if failed.Phase != codexExecServerStopped || failed.Generation != ready.Generation {
			t.Fatalf("executor runtime after failed fence = %+v, want stopped generation %d", failed, ready.Generation)
		}
		retained, generation := r.codexExecServer.currentInstance()
		if retained != instance || generation != ready.Generation {
			t.Fatalf("executor ownership after failed fence = (%p, %d), want retained instance (%p, %d)", retained, generation, instance, ready.Generation)
		}
		if got := starts.Load(); got != 1 || stops.Load() != 1 {
			t.Fatalf("executor starts/stops after failed fence = %d/%d, want 1/1", got, stops.Load())
		}

		if err := r.codexExecServer.request(); err != nil {
			t.Fatalf("publish additional executor demand after failed fence: %v", err)
		}
		synctest.Wait()
		blocked := r.codexExecServer.runtimeState()
		if blocked.Phase != codexExecServerStopped || blocked.Generation != ready.Generation || blocked.StopError == "" || starts.Load() != 1 {
			t.Fatalf("executor advanced after failed fence and new demand: runtime=%+v starts=%d", blocked, starts.Load())
		}

		closeErr := r.Close()
		if closeErr == nil || !strings.Contains(closeErr.Error(), fenceFailure.Error()) {
			t.Fatalf("Runner Close after failed executor fence = %v, want the fence failure", closeErr)
		}
	})
}

func TestExecutorCloseCancelsConcurrentStartupWithoutRestart(t *testing.T) {
	entered := make(chan struct{}, 1)
	var starts, stops atomic.Int32
	r := newExecutorLifecycleRunner(t, func(ctx context.Context) (*codexExecServerInstance, error) {
		starts.Add(1)
		entered <- struct{}{}
		<-ctx.Done()
		return nil, ctx.Err()
	}, nil)

	ensured := make(chan error, 1)
	go func() {
		ctx, cancel := context.WithTimeout(context.Background(), codexExecServerTestTimeout)
		defer cancel()
		ensured <- r.codexExecServer.ensure(ctx)
	}()
	select {
	case <-entered:
	case <-time.After(codexExecServerTestTimeout):
		t.Fatal("executor startup did not begin")
	}
	closed := make(chan error, 1)
	go func() { closed <- r.Close() }()
	select {
	case err := <-closed:
		if err != nil {
			t.Fatalf("close during startup: %v", err)
		}
	case <-time.After(codexExecServerTestTimeout):
		t.Fatal("close did not cancel and join executor startup")
	}
	if err := <-ensured; err == nil {
		t.Fatal("ensure returned ready after close canceled startup")
	}
	if starts.Load() != 1 || stops.Load() != 0 {
		t.Fatalf("startup/stop counts = %d/%d, want one canceled start and no instance to fence", starts.Load(), stops.Load())
	}
	if current := r.codexExecServer.runtimeState(); current.Phase != codexExecServerStopped || current.StopError != "" {
		t.Fatalf("executor runtime after close = %+v, want clean stopped state", current)
	}
}

func newExecutorLifecycleRunner(t *testing.T, start func(context.Context) (*codexExecServerInstance, error), probe func(context.Context) error) *Runner {
	t.Helper()
	dir := t.TempDir()
	socket := filepath.Join(dir, "executor-control.sock")
	companionCtx, cancelCompanion := context.WithCancel(context.Background())
	companionDone := make(chan error, 1)
	go func() {
		companionDone <- serveCodexExecServerContainer(companionCtx, socket, filepath.Join(dir, "unused-codex"), "ws://127.0.0.1:4599", "127.0.0.1:0")
	}()
	waitForSocket(t, socket)

	config := Config{
		StateDir: t.TempDir(),
		Codex:    &harness.LaunchConfig{Binary: "/bin/false"},
		CodexExecServer: &CodexExecServerConfig{
			URL: "ws://127.0.0.1:4599", ContainerSocket: socket,
		},
	}
	r, err := New(config)
	if err != nil {
		cancelCompanion()
		t.Fatalf("create lifecycle Runner: %v", err)
	}
	r.codexExecServer.startEffect, r.codexExecServer.probeEffect = start, probe
	if err := r.Startup(context.Background()); err != nil {
		cancelCompanion()
		t.Fatalf("start lifecycle Runner: %v", err)
	}
	t.Cleanup(func() {
		if err := r.Close(); err != nil {
			t.Errorf("close executor test Runner: %v", err)
		}
		cancelCompanion()
		select {
		case err := <-companionDone:
			if err != nil {
				t.Errorf("stop executor test companion: %v", err)
			}
		case <-time.After(codexExecServerTestTimeout):
			t.Error("executor test companion did not stop")
		}
	})
	return r
}

func newTestExecutorInstance(stops *atomic.Int32) *codexExecServerInstance {
	done := make(chan error, 1)
	var once sync.Once
	return &codexExecServerInstance{
		done: done,
		stop: func(context.Context) error {
			stops.Add(1)
			once.Do(func() { done <- errors.New("test executor was fenced") })
			return nil
		},
	}
}

func forceExecutorProbe(t *testing.T, r *Runner) {
	t.Helper()
	current := r.codexExecServer.runtimeState()
	if current.Phase != codexExecServerReady {
		t.Fatalf("force probe while executor phase is %d", current.Phase)
	}
	current.NextProbe = time.Time{}
	old, err := r.lifecycle.state.Get(r.ctx, resource.NewMetadata(lifecycleNamespace, codexExecServerRuntimeType, codexExecServerResourceID, resource.VersionUndefined))
	if err != nil {
		t.Fatal(err)
	}
	value := lifecycleResourceFor(codexExecServerResourceID, current)
	*value.Metadata() = old.Metadata().Copy()
	if err := r.lifecycle.state.Update(r.ctx, value, state.WithUpdateOwner(codexExecServerControllerName)); err != nil {
		t.Fatal(err)
	}
	if err := r.codexExecServer.request(); err != nil {
		t.Fatal(err)
	}
}

func forceExecutorRetry(t *testing.T, r *Runner) {
	t.Helper()
	current := r.codexExecServer.runtimeState()
	if current.Phase != codexExecServerRetrying {
		t.Fatalf("force retry while executor phase is %d", current.Phase)
	}
	current.RetryAt = time.Time{}
	old, err := r.lifecycle.state.Get(r.ctx, resource.NewMetadata(lifecycleNamespace, codexExecServerRuntimeType, codexExecServerResourceID, resource.VersionUndefined))
	if err != nil {
		t.Fatal(err)
	}
	value := lifecycleResourceFor(codexExecServerResourceID, current)
	*value.Metadata() = old.Metadata().Copy()
	if err := r.lifecycle.state.Update(r.ctx, value, state.WithUpdateOwner(codexExecServerControllerName)); err != nil {
		t.Fatal(err)
	}
	if err := r.codexExecServer.request(); err != nil {
		t.Fatal(err)
	}
}

func waitExecutor(t *testing.T, r *Runner, predicate func(codexExecServerRuntimeSpec) bool, want string) codexExecServerRuntimeSpec {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), codexExecServerTestTimeout)
	defer cancel()
	for {
		changed := r.changesSince()
		current := r.codexExecServer.runtimeState()
		if predicate(current) {
			return current
		}
		select {
		case <-changed:
		case <-ctx.Done():
			t.Fatalf("timed out waiting for %s: executor=%+v", want, current)
		}
	}
}

// A companion whose Codex binary cannot start serves a refusal instead of exiting: a generation that
// spawned nothing has no work to fence, and a container restarting into the same failure would leave
// no socket to fence and a Runner able neither to run Codex nor to quiesce.
func TestCodexExecServerContainerRefusesClaimAndKeepsServing(t *testing.T) {
	dir := t.TempDir()
	socket := filepath.Join(dir, "control.sock")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	served := make(chan error, 1)
	go func() {
		served <- serveCodexExecServerContainer(ctx, socket, filepath.Join(dir, "absent-codex"), "ws://127.0.0.1:4501", "127.0.0.1:0")
	}()
	waitForSocket(t, socket)

	health := httptest.NewServer(codexExecServerContainerHealthHandler(socket))
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
		_, err := claimCodexExecServerContainer(ctx, socket, map[string]string{"PATH": "/usr/bin"})
		var refusal codexExecServerRefusal
		if !errors.As(err, &refusal) {
			t.Fatalf("claim %d: %v (%T), want an codexExecServerRefusal", attempt, err, err)
		}
		if !strings.Contains(err.Error(), "absent-codex") {
			t.Errorf("claim %d: %q, want the companion's reason", attempt, err)
		}
		checkHealth()
	}

	_, status, err := codexExecServerControl(ctx, socket, codexExecServerControlRequest{Operation: "inspect"})
	if err != nil {
		t.Fatalf("inspect after refusals: %v", err)
	}
	if status.Owned || status.Error != "" {
		t.Errorf("inspect after refusals = %+v, want an unowned generation reporting no error", status)
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

// ensure publishes requested demand and observes the controller-owned status.
func (e *codexExecServer) ensure(ctx context.Context) error {
	if err := e.request(); err != nil {
		return err
	}
	return e.runner.lifecycle.waitFor(ctx, func() (bool, error) {
		current := e.runtimeState()
		if current.Phase == codexExecServerReady {
			return true, nil
		}
		if current.Refusal != "" {
			return true, codexExecServerRefusal{reason: current.Refusal}
		}
		if current.Phase == codexExecServerStopped {
			if current.Error != "" {
				return true, errors.New(current.Error)
			}
			if e.runner.lifecycle.desiredState().Mode != runnerRunning {
				return true, errors.New("Codex exec-server is stopped")
			}
		}
		return false, nil
	})
}

func (e *codexExecServer) runtimeState() codexExecServerRuntimeSpec {
	current, _, err := lifecycleRead[codexExecServerRuntimeSpec](e.runner.ctx, e.runner.lifecycle.state, codexExecServerResourceID)
	if err != nil {
		e.runner.fail(err)
	}
	return current
}
