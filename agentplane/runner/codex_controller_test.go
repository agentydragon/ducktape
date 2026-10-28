package runner

import (
	"context"
	"errors"
	"os/exec"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/codex"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
)

func TestCodexJoinedStartupSurvivesTransientExecutorFailure(t *testing.T) {
	var starts, stops atomic.Int32
	r := newExecutorLifecycleRunner(t, func(ctx context.Context) (*executorInstance, error) {
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
			_, err := r.codex.host(ctx, nil)
			results <- err
		}()
	}
	startHostWaiter()
	waitCodexDemand(t, r, 1)
	waitExecutor(t, r, func(current executorRuntimeSpec) bool {
		return current.Phase == executorRetrying && strings.Contains(current.Error, "temporary executor startup failure")
	}, "transient executor startup failure")
	delayExecutorRetry(t, r)
	waitCodex(t, r, func(current codexRuntimeSpec) bool {
		return current.Phase == codexStarting && !current.ExecutorReadyDeadline.IsZero()
	}, "Codex waiting for executor readiness")
	startHostWaiter()
	joined := waitCodexDemand(t, r, 2)
	if joined != 2 {
		t.Fatalf("Codex demand = %d, want both joined callers", joined)
	}
	waiting := codexRuntime(t, r)
	if waiting.ExecutorReadyDeadline.IsZero() {
		t.Fatal("Codex dependency wait lost its startup deadline")
	}
	deadline := waiting.ExecutorReadyDeadline

	// Make the retry immediately eligible. Both Codex callers have joined before the
	// executor becomes ready, so the one shared Codex attempt must satisfy both.
	forceExecutorRetry(t, r)
	waitExecutor(t, r, func(current executorRuntimeSpec) bool { return current.Phase == executorReady }, "executor recovery")
	failed := waitCodex(t, r, func(current codexRuntimeSpec) bool {
		return current.Phase == codexStopped && current.Attempt >= 2 && current.Error != ""
	}, "shared Codex startup attempt")
	if failed.Attempt != 2 {
		t.Errorf("Codex consumed attempt %d, want the joined demand 2", failed.Attempt)
	}
	if !failed.ExecutorReadyDeadline.IsZero() {
		t.Errorf("Codex retained dependency deadline after consuming startup attempt: %s", failed.ExecutorReadyDeadline)
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
	entered := make(chan struct{}, 1)
	r := newExecutorLifecycleRunner(t, func(ctx context.Context) (*executorInstance, error) {
		entered <- struct{}{}
		<-ctx.Done()
		return nil, ctx.Err()
	}, nil)

	if _, err := r.codex.request(nil); err != nil {
		t.Fatalf("publish first Codex demand: %v", err)
	}
	select {
	case <-entered:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("executor startup did not begin")
	}
	waiting := waitCodex(t, r, func(current codexRuntimeSpec) bool {
		return current.Phase == codexStarting && !current.ExecutorReadyDeadline.IsZero()
	}, "initial Codex executor dependency deadline")
	remaining := time.Until(waiting.ExecutorReadyDeadline)
	if remaining < codexExecutorReadyBudget-2*time.Second || remaining > codexExecutorReadyBudget {
		t.Fatalf("initial executor readiness budget = %s, want about %s", remaining, codexExecutorReadyBudget)
	}

	// Expire the original deadline, then publish a joined request. The controller must
	// consume that request against the first waiter's deadline instead of granting it a
	// fresh 30-second startup budget.
	waiting.ExecutorReadyDeadline = time.Now().Add(-time.Millisecond)
	setCodexRuntime(t, r, waiting)
	if _, err := r.codex.request(nil); err != nil {
		t.Fatalf("publish joined Codex demand: %v", err)
	}
	expired := waitCodex(t, r, func(current codexRuntimeSpec) bool {
		return current.Phase == codexStopped && current.Attempt == 2 && strings.Contains(current.Error, "within 30s")
	}, "expired shared executor readiness deadline")
	if !expired.ExecutorReadyDeadline.IsZero() {
		t.Errorf("Codex retained expired dependency deadline: %s", expired.ExecutorReadyDeadline)
	}
}

func TestCodexFailedStopBlocksRunnerFencing(t *testing.T) {
	r := newCodexOnlyRunner(t)
	entered, release := make(chan struct{}), make(chan struct{})
	stopFailure := errors.New("fake Codex process would not stop")
	r.codex.mu.Lock()
	r.codex.current = &codexProcess{
		id:      "test-process",
		process: &Process{started: true, cmd: &exec.Cmd{}, waitDone: make(chan struct{})},
		router:  codex.NewRouter[*Session](),
	}
	r.codex.stopEffect = func(context.Context) error {
		close(entered)
		<-release
		return stopFailure
	}
	r.codex.mu.Unlock()
	setCodexRuntime(t, r, codexRuntimeSpec{
		Phase: codexReady, Generation: r.lifecycle.goal().Generation, Attempt: 1, ProcessID: "test-process",
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

	close(release)
	if err := waitClose(t, closed); !errors.Is(err, stopFailure) && !strings.Contains(err.Error(), stopFailure.Error()) {
		t.Fatalf("Runner Close = %v, want Codex stop failure", err)
	}
	failed := waitCodex(t, r, func(current codexRuntimeSpec) bool {
		return current.Phase == codexStopped && current.StopError != ""
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
	p := &codexProcess{
		id:      "test-process",
		process: &Process{started: true, cmd: &exec.Cmd{}, waitDone: make(chan struct{})},
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

func codexRuntime(t *testing.T, r *Runner) codexRuntimeSpec {
	t.Helper()
	current, _, err := lifecycleRead[codexRuntimeSpec](r.ctx, r.lifecycle.state, codexRuntimeType, "codex")
	if err != nil {
		t.Fatal(err)
	}
	return current
}

func waitCodex(t *testing.T, r *Runner, predicate func(codexRuntimeSpec) bool, want string) codexRuntimeSpec {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	for {
		changed := r.changesSince()
		current := codexRuntime(t, r)
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

func codexDemand(t *testing.T, r *Runner) uint64 {
	t.Helper()
	input, found, err := lifecycleRead[codexInputSpec](r.ctx, r.lifecycle.state, codexInputType, "codex")
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
		if got := codexDemand(t, r); got >= want {
			return got
		}
		select {
		case <-ticker.C:
		case <-ctx.Done():
			t.Fatalf("timed out waiting for Codex demand %d", want)
		}
	}
}

func setCodexRuntime(t *testing.T, r *Runner, current codexRuntimeSpec) {
	t.Helper()
	old, err := r.lifecycle.state.Get(r.ctx, resource.NewMetadata(lifecycleNamespace, codexRuntimeType, "codex", resource.VersionUndefined))
	if err != nil {
		t.Fatal(err)
	}
	value := lifecycleResourceFor(codexRuntimeType, "codex", current)
	value.metadata = old.Metadata().Copy()
	if err := r.lifecycle.state.Update(r.ctx, value, state.WithUpdateOwner(codexControllerName)); err != nil {
		t.Fatal(err)
	}
}

func delayExecutorRetry(t *testing.T, r *Runner) {
	t.Helper()
	current := r.executor.runtimeState()
	if current.Phase != executorRetrying {
		t.Fatalf("delay executor retry while phase=%d", current.Phase)
	}
	current.RetryAt = time.Now().Add(time.Minute)
	old, err := r.lifecycle.state.Get(r.ctx, resource.NewMetadata(lifecycleNamespace, executorRuntimeType, "executor", resource.VersionUndefined))
	if err != nil {
		t.Fatal(err)
	}
	value := lifecycleResourceFor(executorRuntimeType, "executor", current)
	value.metadata = old.Metadata().Copy()
	if err := r.lifecycle.state.Update(r.ctx, value, state.WithUpdateOwner(executorControllerName)); err != nil {
		t.Fatal(err)
	}
	if err := r.executor.request(); err != nil {
		t.Fatal(err)
	}
	waitExecutor(t, r, func(latest executorRuntimeSpec) bool {
		return latest.Phase == executorRetrying && latest.RetryAt == current.RetryAt
	}, "delayed executor retry")
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
