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

	"github.com/agentydragon/ducktape/agentplane/runner/codex"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
)

func TestCodexJoinedStartupSurvivesTransientExecutorFailure(t *testing.T) {
	var starts, stops atomic.Int32
	r := newExecutorLifecycleRunner(t, func(ctx context.Context) (*codexExecServerInstance, error) {
		if starts.Add(1) == 1 {
			return nil, errors.New("temporary executor startup failure")
		}
		return newTestExecutorInstance(&stops), nil
	}, func(context.Context) error { return nil })

	results := make(chan error, 2)
	startHostWaiter := func() {
		go func() {
			ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
			defer cancel()
			_, err := r.codexAppServer.host(ctx, nil)
			results <- err
		}()
	}
	startHostWaiter()
	waitCodexDemand(t, r, 1)
	waitExecutor(t, r, func(current codexExecServerRuntimeSpec) bool {
		return current.Phase == codexExecServerRetrying && strings.Contains(current.Error, "temporary executor startup failure")
	}, "transient executor startup failure")
	delayExecutorRetry(t, r)
	waitCodex(t, r, func(current codexAppServerRuntimeSpec) bool {
		return current.Phase == codexAppServerStarting && !current.ExecServerReadyDeadline.IsZero()
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

	// Make the retry immediately eligible. Both Codex callers have joined before the
	// executor becomes ready, so the one shared Codex attempt must satisfy both.
	forceExecutorRetry(t, r)
	waitExecutor(t, r, func(current codexExecServerRuntimeSpec) bool { return current.Phase == codexExecServerReady }, "executor recovery")
	failed := waitCodex(t, r, func(current codexAppServerRuntimeSpec) bool {
		return current.Phase == codexAppServerStopped && current.Attempt >= 2 && current.Error != ""
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
		r := newCodexSynctestRunner(t, stateDir, func(ctx context.Context) (*codexExecServerInstance, error) {
			enteredOnce.Do(func() { close(entered) })
			<-ctx.Done()
			return nil, ctx.Err()
		})

		if _, err := r.codexAppServer.request(nil); err != nil {
			t.Fatalf("publish first Codex demand: %v", err)
		}
		synctest.Wait()
		select {
		case <-entered:
		default:
			t.Fatal("executor startup did not begin")
		}
		initial := codexAppServerRuntime(t, r)
		if initial.Phase != codexAppServerStarting || initial.ExecServerReadyDeadline.IsZero() {
			t.Fatalf("Codex initial dependency state = %+v, want a 30s executor wait", initial)
		}
		if remaining := initial.ExecServerReadyDeadline.Sub(time.Now()); remaining != codexAppServerDependencyBudget {
			t.Fatalf("initial executor readiness budget = %s, want %s", remaining, codexAppServerDependencyBudget)
		}

		time.Sleep(10 * time.Second)
		synctest.Wait()
		if _, err := r.codexAppServer.request(nil); err != nil {
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
		if expired.Phase != codexAppServerStopped || expired.Attempt != 2 || !strings.Contains(expired.Error, "within 30s") {
			t.Fatalf("Codex state at original 30s deadline = %+v, want demand 2 consumed", expired)
		}
		if !expired.ExecServerReadyDeadline.IsZero() {
			t.Errorf("Codex retained expired dependency deadline: %s", expired.ExecServerReadyDeadline)
		}
		if err := r.Close(); err != nil {
			t.Fatalf("close synctest Runner: %v", err)
		}
	})
}

func TestCodexFailedStopBlocksRunnerFencing(t *testing.T) {
	r := newCodexOnlyRunner(t)
	entered, release := make(chan struct{}), make(chan struct{})
	var releaseOnce sync.Once
	releaseStop := func() { releaseOnce.Do(func() { close(release) }) }
	t.Cleanup(releaseStop)
	stopFailure := errors.New("fake Codex process would not stop")
	r.codexAppServer.mu.Lock()
	r.codexAppServer.current = &codexAppServerProcess{
		id:      "test-process",
		process: newControllerTestProcess(t),
		router:  codex.NewRouter[*Session](),
	}
	r.codexAppServer.stopEffect = func(context.Context) error {
		close(entered)
		<-release
		return stopFailure
	}
	r.codexAppServer.mu.Unlock()
	setCodexRuntime(t, r, codexAppServerRuntimeSpec{
		Phase: codexAppServerReady, Generation: r.lifecycle.desiredState().Generation, Attempt: 1, ProcessID: "test-process",
	})

	closed := make(chan error, 1)
	go func() { closed <- r.Close() }()
	select {
	case <-entered:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("COSI did not start the Codex stop operation")
	}
	waitLifecycle(t, r, func() bool { return r.lifecycle.status().Stage >= stageStopping }, "Runner stopping barrier")
	if stage := r.lifecycle.status().Stage; stage >= stageFencing {
		t.Fatalf("Runner crossed fencing while Codex stop was unresolved: stage=%d", stage)
	}
	select {
	case err := <-closed:
		t.Fatalf("Runner Close returned before Codex stop settled: %v", err)
	default:
	}

	releaseStop()
	if err := waitClose(t, closed); err == nil || !errors.Is(err, stopFailure) && !strings.Contains(err.Error(), stopFailure.Error()) {
		t.Fatalf("Runner Close = %v, want Codex stop failure", err)
	}
	failed := waitCodex(t, r, func(current codexAppServerRuntimeSpec) bool {
		return current.Phase == codexAppServerStopped && current.StopError != ""
	}, "Codex stop failure")
	if !strings.Contains(failed.StopError, stopFailure.Error()) {
		t.Errorf("Codex StopError = %q, want %q", failed.StopError, stopFailure)
	}
	if stage := r.lifecycle.status().Stage; stage >= stageFencing {
		t.Errorf("Runner crossed fencing after failed Codex stop: stage=%d", stage)
	}
	forceStopTestRunner(t, r)
}

func TestCodexExitSnapshotRejectsLateSessionAttachment(t *testing.T) {
	owner := &Session{id: "already-hosted"}
	late := &Session{id: "late-attachment"}
	p := &codexAppServerProcess{
		id:      "test-process",
		process: newControllerTestProcess(t),
		router:  codex.NewRouter[*Session](),
	}
	p.router.Host(owner)
	if !p.host(late) {
		t.Fatal("live process rejected an attachment before exit cutover")
	}

	hosts := p.beginExit()
	if p.host(&Session{id: "after-cutover"}) {
		t.Fatal("Codex process accepted a Session after exit cutover")
	}
	if !containsSession(hosts, late) {
		t.Errorf("exit snapshot omitted a Session attached before cutover: %v", sessionIDs(hosts))
	}
	if !containsSession(hosts, owner) {
		t.Errorf("exit snapshot omitted the startup-frame owner: %v", sessionIDs(hosts))
	}
	if len(hosts) != 2 {
		t.Errorf("exit snapshot hosts = %v, want only the two pre-cutover Sessions", sessionIDs(hosts))
	}
}

func newCodexOnlyRunner(t *testing.T) *Runner {
	t.Helper()
	r, err := New(Config{StateDir: t.TempDir(), Codex: &harness.LaunchConfig{Binary: "/bin/false"}})
	if err != nil {
		t.Fatal(err)
	}
	if err := r.store.AdoptIncarnation("codex-controller-test"); err != nil {
		_ = r.owner.Close()
		t.Fatal(err)
	}
	if err := r.Startup(context.Background()); err != nil {
		_ = r.owner.Close()
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if r.closeErr == nil {
			if err := r.Close(); err != nil {
				t.Errorf("close Codex controller test Runner: %v", err)
			}
		}
		forceStopTestRunner(t, r)
	})
	return r
}

func newCodexSynctestRunner(t *testing.T, stateDir string, startEffect func(context.Context) (*codexExecServerInstance, error)) *Runner {
	t.Helper()
	owner, err := OpenStateOwner(stateDir)
	if err != nil {
		t.Fatal(err)
	}
	store, err := NewSessionStore(filepath.Join(stateDir, "sessions"))
	if err != nil {
		_ = owner.Close()
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	codexExecServerConfig := CodexExecServerConfig{URL: "ws://127.0.0.1:4599"}
	codexConfig := harness.LaunchConfig{Binary: "/bin/false"}
	config := Config{StateDir: stateDir, Codex: &codexConfig, CodexExecServer: &codexExecServerConfig}
	r := &Runner{
		config: config, ctx: ctx, cancel: cancel, owner: owner, store: store,
		sessions: map[string]*Session{}, changes: make(chan struct{}), fatal: make(chan error, 1),
	}
	// Configure every native host before lifecycle controllers or their goroutines exist.
	r.codexExecServer = newCodexExecServer(r, codexExecServerConfig)
	r.codexExecServer.startEffect = startEffect
	r.codexExecServer.probeEffect = func(context.Context) error { return nil }
	r.codexAppServer = newCodexAppServer(r, codexConfig)
	lifecycle, err := newLifecycle(r)
	if err != nil {
		cancel()
		_ = owner.Close()
		t.Fatal(err)
	}
	r.lifecycle = lifecycle
	go lifecycle.run()
	if err := r.store.AdoptIncarnation("codex-synctest"); err != nil {
		t.Fatal(err)
	}
	if err := r.Startup(context.Background()); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if r.closeErr == nil {
			if err := r.Close(); err != nil {
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
	case <-r.lifecycle.done:
	default:
		r.cancel()
		<-r.lifecycle.done
	}
	r.lifecycle.workers.Wait()
	if err := r.owner.Close(); err != nil {
		t.Errorf("close test Runner state owner: %v", err)
	}
}

func codexAppServerRuntime(t *testing.T, r *Runner) codexAppServerRuntimeSpec {
	t.Helper()
	current, _, err := lifecycleRead[codexAppServerRuntimeSpec](r.ctx, r.lifecycle.state, codexAppServerResourceID)
	if err != nil {
		t.Fatal(err)
	}
	return current
}

func waitCodex(t *testing.T, r *Runner, predicate func(codexAppServerRuntimeSpec) bool, want string) codexAppServerRuntimeSpec {
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
	input, found, err := lifecycleRead[codexAppServerInputSpec](r.ctx, r.lifecycle.state, codexAppServerResourceID)
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

func setCodexRuntime(t *testing.T, r *Runner, current codexAppServerRuntimeSpec) {
	t.Helper()
	updateLifecycleValueForTest(t, r, codexAppServerResourceID, codexAppServerControllerName, current)
}

func delayExecutorRetry(t *testing.T, r *Runner) {
	t.Helper()
	current := r.codexExecServer.runtimeState()
	if current.Phase != codexExecServerRetrying {
		t.Fatalf("delay executor retry while phase=%d", current.Phase)
	}
	current.RetryAt = time.Now().Add(time.Minute)
	updateLifecycleValueForTest(t, r, codexExecServerResourceID, codexExecServerControllerName, current)
	if err := r.codexExecServer.request(); err != nil {
		t.Fatal(err)
	}
	waitExecutor(t, r, func(latest codexExecServerRuntimeSpec) bool {
		return latest.Phase == codexExecServerRetrying && latest.RetryAt == current.RetryAt
	}, "delayed executor retry")
}

func updateLifecycleValueForTest[T lifecycleSpec[T]](t *testing.T, r *Runner, id, owner string, spec T) {
	t.Helper()
	ctx, cancel := context.WithTimeout(r.ctx, lifecycleTestTimeout)
	defer cancel()
	if err := modifyLifecycleForTest(ctx, r, id, owner, func(current *T) { *current = spec }); err != nil {
		t.Fatal(err)
	}
}

func containsSession(sessions []*Session, target *Session) bool {
	for _, session := range sessions {
		if session == target {
			return true
		}
	}
	return false
}

func sessionIDs(sessions []*Session) []string {
	ids := make([]string, 0, len(sessions))
	for _, session := range sessions {
		ids = append(ids, session.id)
	}
	return ids
}

func newControllerTestProcess(t *testing.T) *supervision.Process {
	t.Helper()
	process, err := supervision.NewSubprocessSupervisor(nil).NewProcess(supervision.ProcessConfig{
		Command: []string{"/bin/sleep", "60"},
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	if err := process.Start(ctx); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := process.Stop(context.Background(), 0); err != nil {
			t.Error(err)
		}
	})
	return process
}
