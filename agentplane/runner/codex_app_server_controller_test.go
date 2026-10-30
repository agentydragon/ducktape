package runner

import (
	"context"
	"errors"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"testing/synctest"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/codexappserver"
	"github.com/agentydragon/ducktape/agentplane/runner/codexexecserver"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	"github.com/agentydragon/ducktape/agentplane/runner/scripts"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
)

func TestCodexJoinedStartupSurvivesTransientExecutorFailure(t *testing.T) {
	var starts, stops atomic.Int32
	recoveryStarted := make(chan struct{})
	releaseRecovery := make(chan struct{})
	r := newExecutorLifecycleRunner(t, func(ctx context.Context) (*supervision.Instance, error) {
		if starts.Add(1) == 1 {
			return nil, errors.New("temporary executor startup failure")
		}
		close(recoveryStarted)
		select {
		case <-releaseRecovery:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
		return newTestExecutorInstance(&stops), nil
	}, func(context.Context) error { return nil })

	results := make(chan error, 2)
	startHostWaiter := func() {
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
			defer cancel()
			_, err := r.codexAppServer.Host(ctx, nil)
			results <- err
		}()
	}
	startHostWaiter()
	waitCodexDemand(t, r, 1)
	waitExecutor(t, r, func(current codexexecserver.Runtime) bool {
		return current.Phase == codexexecserver.Retrying && strings.Contains(current.Error, "temporary executor startup failure")
	}, "transient executor startup failure")
	waitCodex(t, r, func(current codexappserver.Runtime) bool {
		return current.Phase == codexappserver.Initializing && !current.ExecServerReadyDeadline.IsZero()
	}, "Codex waiting for executor readiness")
	startHostWaiter()
	joined := waitCodexDemand(t, r, 2)
	if joined != 2 {
		t.Fatalf("Codex demand = %d, want both joined callers", joined)
	}
	waiting := codexAppServerRuntime(t, r)
	if waiting.ExecServerReadyDeadline.IsZero() {
		t.Fatal("Codex dependency wait lost its startup deadline")
	}
	deadline := waiting.ExecServerReadyDeadline

	// Hold the retry's native start until both Codex callers have joined.
	select {
	case <-recoveryStarted:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("executor recovery did not reach native start")
	}
	close(releaseRecovery)
	waitExecutor(t, r, func(current codexexecserver.Runtime) bool { return current.Phase == codexexecserver.Ready }, "executor recovery")
	failed := waitCodex(t, r, func(current codexappserver.Runtime) bool {
		return current.Phase == codexappserver.Stopped && current.Attempt >= 2 && current.Error != ""
	}, "shared Codex startup attempt")
	if failed.Attempt != 2 {
		t.Errorf("Codex consumed attempt %d, want the joined demand 2", failed.Attempt)
	}
	if !failed.ExecServerReadyDeadline.IsZero() {
		t.Errorf("Codex retained dependency deadline after consuming startup attempt: %s", failed.ExecServerReadyDeadline)
	}
	if deadline.IsZero() {
		t.Fatal("joined startup did not have a persistent executor deadline")
	}
	for i := range 2 {
		select {
		case err := <-results:
			if err == nil {
				t.Errorf("Codex waiter %d succeeded although the test server exits during initialization", i+1)
			}
		case <-time.After(lifecycleTestTimeout):
			t.Fatalf("Codex waiter %d did not observe the shared startup result", i+1)
		}
	}
	if got := starts.Load(); got != 2 {
		t.Errorf("executor start count = %d, want one transient failure and one recovery", got)
	}
}

func TestCodexExecutorReadinessDeadlineConsumesJoinedDemand(t *testing.T) {
	stateDir := t.TempDir()
	synctest.Test(t, func(t *testing.T) {
		entered := make(chan struct{})
		var enteredOnce sync.Once
		r := newCodexSynctestRunner(t, stateDir, func(ctx context.Context) (*supervision.Instance, error) {
			enteredOnce.Do(func() { close(entered) })
			<-ctx.Done()
			return nil, ctx.Err()
		})

		if _, err := requestCodexAppServer(r); err != nil {
			t.Fatalf("publish first Codex demand: %v", err)
		}
		synctest.Wait()
		select {
		case <-entered:
		default:
			t.Fatal("executor startup did not begin")
		}
		initial := codexAppServerRuntime(t, r)
		if initial.Phase != codexappserver.Initializing || initial.ExecServerReadyDeadline.IsZero() {
			t.Fatalf("Codex initial dependency state = %+v, want a 30s executor wait", initial)
		}
		if remaining := initial.ExecServerReadyDeadline.Sub(time.Now()); remaining != 30*time.Second {
			t.Fatalf("initial executor readiness budget = %s, want %s", remaining, 30*time.Second)
		}

		time.Sleep(10 * time.Second)
		synctest.Wait()
		if _, err := requestCodexAppServer(r); err != nil {
			t.Fatalf("publish joined Codex demand: %v", err)
		}
		synctest.Wait()
		joined := codexAppServerRuntime(t, r)
		if joined.ExecServerReadyDeadline != initial.ExecServerReadyDeadline {
			t.Fatalf("joined demand changed executor deadline: got %s, want original %s", joined.ExecServerReadyDeadline, initial.ExecServerReadyDeadline)
		}
		if demand := codexAppServerDemand(t, r); demand != 2 {
			t.Fatalf("joined Codex demand = %d, want 2", demand)
		}

		time.Sleep(20 * time.Second)
		synctest.Wait()
		expired := codexAppServerRuntime(t, r)
		if expired.Phase != codexappserver.Stopped || expired.Attempt != 2 || !strings.Contains(expired.Error, "within 30s") {
			t.Fatalf("Codex state at original 30s deadline = %+v, want demand 2 consumed", expired)
		}
		if !expired.ExecServerReadyDeadline.IsZero() {
			t.Errorf("Codex retained expired dependency deadline: %s", expired.ExecServerReadyDeadline)
		}
		if err := r.Close(context.Background()); err != nil {
			t.Fatalf("close synctest Runner: %v", err)
		}
	})
}

func newCodexSynctestRunner(t *testing.T, stateDir string, startEffect func(context.Context) (*supervision.Instance, error)) *Runner {
	t.Helper()
	owner, err := retained.OpenOwner(stateDir)
	if err != nil {
		t.Fatal(err)
	}
	store, err := retained.NewStore(filepath.Join(stateDir, "sessions"))
	if err != nil {
		_ = owner.Close()
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	codexExecServerConfig := codexexecserver.Config{URL: "ws://127.0.0.1:4599"}
	codexConfig := harness.LaunchConfig{Binary: "/bin/false"}
	config := Config{StateDir: stateDir, Codex: &codexConfig, CodexExecServer: &codexExecServerConfig}
	r := &Runner{
		config: config, ctx: ctx, cancel: cancel, owner: owner, store: store,
		changes: make(chan struct{}), fatal: make(chan error, 1),
	}
	// Configure every native host before lifecycle controllers or their goroutines exist.
	r.supervisor = supervision.NewSubprocessSupervisor(owner.File())
	r.scripts, err = scripts.New(scripts.Config{
		LaunchProcess: func(ctx context.Context, owner string, config supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
			return r.launchProcess(ctx, owner, r.supervisor, config)
		},
	})
	if err != nil {
		cancel()
		_ = owner.Close()
		t.Fatal(err)
	}
	adapters := codexexecserver.Adapters{Start: startEffect, Probe: func(context.Context) error { return nil }}
	lifecycle, err := newLifecycle(r, &adapters, nil)
	if err != nil {
		cancel()
		_ = owner.Close()
		t.Fatal(err)
	}
	r.lifecycle = lifecycle
	if err := r.configureSessions(); err != nil {
		cancel()
		_ = owner.Close()
		t.Fatal(err)
	}
	r.configureServing()
	go lifecycle.run()
	if err := r.store.AdoptIncarnation("codex-synctest"); err != nil {
		t.Fatal(err)
	}
	if err := r.Startup(context.Background()); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if r.closeErr == nil {
			if err := r.Close(context.Background()); err != nil {
				t.Errorf("close Codex synctest Runner: %v", err)
			}
		}
		forceStopTestRunner(t, r)
	})
	return r
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
