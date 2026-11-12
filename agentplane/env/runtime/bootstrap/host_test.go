package bootstrap

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/agentydragon/ducktape/agentplane/env/scripts"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"go.uber.org/zap"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func openTestState(stateDir string) (state.State, io.Closer, error) {
	return cosistate.Open(stateDir, lifecycle.Namespace, SelectionRegistration(), EventRegistration(), retained.SessionRecordRegistration(), retained.IngressAckRegistration())
}

const bootstrapTestTimeout = 10 * time.Second

type initialOperationFailureState struct {
	state.State
	err error
}

type operationSignalState struct {
	state.State
	revision   uint64
	err        error
	publishing chan struct{}
	release    <-chan struct{}
	once       sync.Once
}

func (s *operationSignalState) UpdateWithConflicts(ctx context.Context, ptr resource.Pointer, update state.UpdaterFunc, opts ...state.UpdateOption) (resource.Resource, error) {
	return s.State.UpdateWithConflicts(ctx, ptr, func(value resource.Resource) error {
		if err := update(value); err != nil {
			return err
		}
		signal, ok := value.(*typed.Resource[operationSignal, lifecycle.Extension[operationSignal]])
		if !ok || signal.TypedSpec().Revision != s.revision {
			return nil
		}
		if s.publishing != nil {
			s.once.Do(func() { close(s.publishing) })
		}
		if s.release != nil {
			select {
			case <-s.release:
			case <-ctx.Done():
				return ctx.Err()
			}
		}
		return s.err
	}, opts...)
}

type selectionPublicationFailureState struct {
	state.State
	err error
}

type failedBackingStore struct{ err error }

func (failedBackingStore) Load(context.Context, inmem.LoadHandler) error { return nil }
func (s failedBackingStore) Put(context.Context, resource.Type, resource.Resource) error {
	return s.err
}

func (failedBackingStore) Destroy(context.Context, resource.Type, resource.Pointer) error { return nil }

func TestFailedDurablePutDoesNotExposeSelection(t *testing.T) {
	failure := errors.New("backing store write failed")
	backing := failedBackingStore{err: failure}
	build := inmem.NewStateWithOptions(inmem.WithBackingStore(backing))
	store := state.WrapCore(namespaced.NewState(func(ns resource.Namespace) state.CoreState { return build(ns) }))
	if err := publishSelection(t.Context(), store, "printf effect"); !errors.Is(err, failure) {
		t.Fatalf("selection write = %v, want backing store error", err)
	}
	if _, found, err := ReadSelection(t.Context(), store); err != nil || found {
		t.Fatalf("selection after failed durable write = (%t, %v), want absent", found, err)
	}
}

func TestFailedDurablePutDoesNotExposeSession(t *testing.T) {
	failure := errors.New("backing store write failed")
	build := inmem.NewStateWithOptions(inmem.WithBackingStore(failedBackingStore{err: failure}))
	resources := state.WrapCore(namespaced.NewState(func(ns resource.Namespace) state.CoreState { return build(ns) }))
	sessions, err := retained.NewStore(t.TempDir(), resources)
	if err != nil {
		t.Fatal(err)
	}
	record, err := retained.NewRecord(&sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: "sandbox", Uid: "sandbox-uid"}, Harness: sessionpb.Harness_HARNESS_CLAUDE}}}, Cwd: "/workspace", Model: "model"}, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := sessions.Write("session-1", record); !errors.Is(err, failure) {
		t.Fatalf("write = %v, want backing failure", err)
	}
	if exists, err := sessions.Exists("session-1"); err != nil || exists {
		t.Fatalf("Session after failed write = %t, %v", exists, err)
	}
}

func (s selectionPublicationFailureState) Modify(ctx context.Context, value resource.Resource, update func(resource.Resource) error, opts ...state.UpdateOption) error {
	if value.Metadata().Type() == SelectionType {
		return s.err
	}
	return s.State.Modify(ctx, value, update, opts...)
}

func TestSelectionPublicationFailureDoesNotLaunch(t *testing.T) {
	stateDir := t.TempDir()
	store, closer, err := openTestState(stateDir)
	if err != nil {
		t.Fatal(err)
	}
	defer closer.Close()
	failure := errors.New("selection publication failed")
	var launched atomic.Bool
	runner, err := scripts.New(scripts.Config{LaunchProcess: func(context.Context, string, supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
		launched.Store(true)
		return nil, errors.New("unexpected script launch")
	}})
	if err != nil {
		t.Fatal(err)
	}
	script := "printf effect"
	host := &Host{config: Config{Script: &script, StateDir: stateDir}, state: selectionPublicationFailureState{State: store, err: failure}, scripts: runner}
	if err := host.runBootstrap(t.Context()); !errors.Is(err, failure) {
		t.Fatalf("selection failure = %v, want publication error", err)
	}
	if launched.Load() {
		t.Fatal("script launched without a durable selection")
	}
	if _, found, err := ReadSelection(t.Context(), store); err != nil || found {
		t.Fatalf("selection after failed publication = (%t, %v), want absent", found, err)
	}
}

func (s initialOperationFailureState) UpdateWithConflicts(ctx context.Context, ptr resource.Pointer, update state.UpdaterFunc, opts ...state.UpdateOption) (resource.Resource, error) {
	return s.State.UpdateWithConflicts(ctx, ptr, func(value resource.Resource) error {
		if err := update(value); err != nil {
			return err
		}
		if result, ok := value.(*typed.Resource[operationSignal, lifecycle.Extension[operationSignal]]); ok && result.TypedSpec().Revision == 1 {
			return s.err
		}
		return nil
	}, opts...)
}

func TestControllerInitialOperationPublicationFailureBecomesTerminal(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), bootstrapTestTimeout)
	defer cancel()
	publicationErr := errors.New("initial operation state write failed")
	workers := &sync.WaitGroup{}
	reported := make(chan error, 4)
	underlying := state.WrapCore(namespaced.NewState(inmem.Build))
	store := initialOperationFailureState{State: underlying, err: publicationErr}
	var effects atomic.Int32
	script := "exit 0"
	scriptRunner, err := scripts.New(scripts.Config{LaunchProcess: func(context.Context, string, supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
		effects.Add(1)
		return nil, errors.New("bootstrap script should not launch")
	}})
	if err != nil {
		t.Fatal(err)
	}
	host, err := NewHost(Config{Script: &script, StateDir: t.TempDir()}, Dependencies{
		Context: ctx, State: store, Workers: workers, Scripts: scriptRunner,
		Admission: func(context.Context, lifecycle.EnvironmentDesired, func(context.Context) error) error { return nil },
		OnError:   func(err error) { reported <- err },
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := host.Initialize(ctx); err != nil {
		t.Fatal(err)
	}
	if err := lifecycle.Publish(ctx, store, ResourceID, Request{Requested: true}, HostOwner); err != nil {
		t.Fatal(err)
	}
	engine, err := runtime.NewRuntime(store, zap.NewNop())
	if err != nil {
		t.Fatal(err)
	}
	if err := engine.RegisterQController(host.Controller()); err != nil {
		t.Fatal(err)
	}
	runDone := make(chan error, 1)
	go func() { runDone <- engine.Run(ctx) }()
	t.Cleanup(func() {
		cancel()
		select {
		case err := <-runDone:
			if err != nil && !errors.Is(err, context.Canceled) {
				t.Errorf("bootstrap controller runtime: %v", err)
			}
		case <-time.After(bootstrapTestTimeout):
			t.Error("bootstrap controller runtime did not stop")
		}
		workers.Wait()
	})
	if _, err := safe.StateWatchFor[*typed.Resource[Status, lifecycle.Extension[Status]]](
		ctx, store,
		lifecycle.ResourceFor(ResourceID, Status{}).Metadata(), state.WithEventTypes(state.Created, state.Updated),
		state.WithCondition(func(value resource.Resource) (bool, error) {
			current, ok := value.(*typed.Resource[Status, lifecycle.Extension[Status]])
			if !ok {
				return false, fmt.Errorf("unexpected bootstrap status resource %T", value)
			}
			return current.TypedSpec().Phase == Failed, nil
		}),
	); err != nil {
		t.Fatal(err)
	}
	workers.Wait()
	if got := effects.Load(); got != 0 {
		t.Fatalf("bootstrap effects = %d, want 0", got)
	}
	current, found, err := ReadStatus(ctx, store)
	if err != nil || !found || current.Phase != Failed || current.Error == "" {
		t.Fatalf("bootstrap status = %+v, found=%t, error=%v", current, found, err)
	}
	settled, err := host.Settled(ctx)
	if err != nil || !settled {
		t.Fatalf("bootstrap settled = %t, error = %v", settled, err)
	}
	host.mu.Lock()
	slot := host.operation
	host.mu.Unlock()
	if slot != nil {
		t.Fatal("operation slot remained owned after initial publication failure")
	}
	select {
	case err := <-reported:
		if !errors.Is(err, publicationErr) {
			t.Fatalf("reported error = %v, want initial publication failure", err)
		}
	case <-time.After(bootstrapTestTimeout):
		t.Fatal("initial publication failure was not reported")
	}
}

type hostTestRuntime struct {
	ctx          context.Context
	cancel       context.CancelFunc
	state        state.State
	storeCloser  io.Closer
	host         *Host
	processes    *supervision.Host
	workers      *sync.WaitGroup
	engineCancel context.CancelFunc
	done         chan struct{}
	failures     chan error
	closeOnce    sync.Once
}

func newHostTestRuntime(t *testing.T, script *string, stateDir string, admission lifecycle.EnvironmentAdmissionGuard) *hostTestRuntime {
	t.Helper()

	ctx, cancel := context.WithCancel(context.Background())
	store, storeCloser, err := openTestState(stateDir)
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	workers := &sync.WaitGroup{}
	failures := make(chan error, 8)
	reportError := func(err error) {
		if err == nil {
			return
		}
		select {
		case failures <- err:
		default:
		}
	}
	processes := supervision.NewHost(ctx, store, lifecycle.Namespace, supervision.RuntimeDomain, workers, reportError)
	supervisor, err := supervision.NewPID1Supervisor()
	if err != nil {
		t.Fatal(err)
	}
	scriptRunner, err := scripts.New(scripts.Config{
		LaunchProcess: func(ctx context.Context, owner string, config supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
			return processes.Launch(ctx, owner, 1, supervisor, config)
		},
	})
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	if admission == nil {
		admission = func(ctx context.Context, expected lifecycle.EnvironmentDesired, effect func(context.Context) error) error {
			current, found, err := lifecycle.ReadEnvironmentDesired(ctx, store)
			if err != nil {
				return err
			}
			if !found || current != expected || current.Mode != lifecycle.Running {
				return nil
			}
			return effect(ctx)
		}
	}
	host, err := NewHost(Config{Script: script, StateDir: stateDir}, Dependencies{
		Context: ctx, State: store, Workers: workers, Scripts: scriptRunner,
		Admission: admission, OnError: reportError,
	})
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	if err := lifecycle.PublishEnvironmentDesired(ctx, store, lifecycle.EnvironmentDesired{Mode: lifecycle.Running, Generation: 1}); err != nil {
		cancel()
		t.Fatal(err)
	}
	if err := host.Initialize(ctx); err != nil {
		cancel()
		t.Fatal(err)
	}
	testRuntime := &hostTestRuntime{
		ctx: ctx, cancel: cancel, state: store, storeCloser: storeCloser, host: host, processes: processes, workers: workers,
		done: make(chan struct{}), failures: failures,
	}
	testRuntime.startController(t)
	t.Cleanup(func() { testRuntime.close(t) })
	return testRuntime
}

func (r *hostTestRuntime) startController(t *testing.T) {
	t.Helper()
	engine, err := runtime.NewRuntime(r.state, zap.L().Named("bootstrap-component-test"))
	if err != nil {
		t.Fatal(err)
	}
	if err := engine.RegisterQController(r.host.Controller()); err != nil {
		t.Fatal(err)
	}
	if err := engine.RegisterQController(r.processes.Controller()); err != nil {
		t.Fatal(err)
	}
	engineCtx, engineCancel := context.WithCancel(r.ctx)
	r.engineCancel = engineCancel
	r.done = make(chan struct{})
	go func() {
		defer close(r.done)
		if err := engine.Run(engineCtx); err != nil && !errors.Is(err, context.Canceled) {
			select {
			case r.failures <- err:
			default:
			}
		}
	}()
}

func (r *hostTestRuntime) stopController(t *testing.T) {
	t.Helper()
	r.engineCancel()
	select {
	case <-r.done:
	case <-time.After(bootstrapTestTimeout):
		t.Fatal("bootstrap controller runtime did not stop")
	}
}

func (r *hostTestRuntime) close(t *testing.T) {
	t.Helper()
	r.closeOnce.Do(func() {
		r.cancel()
		<-r.done
		r.workers.Wait()
		if err := r.storeCloser.Close(); err != nil {
			t.Errorf("close bootstrap state: %v", err)
		}
		for {
			select {
			case err := <-r.failures:
				t.Errorf("bootstrap test runtime failed: %v", err)
			default:
				return
			}
		}
	})
}

func TestWaitCancellationDoesNotCancelAttemptAndDurableSuccessDoesNotRerun(t *testing.T) {
	stateDir := t.TempDir()
	started := filepath.Join(stateDir, "started")
	release := filepath.Join(stateDir, "release")
	count := filepath.Join(stateDir, "count")
	script := fmt.Sprintf(": > %q\nwhile [ ! -f %q ]; do sleep 0.01; done\nprintf b >> %q\nprintf ready", started, release, count)
	first := newHostTestRuntime(t, &script, stateDir, nil)
	t.Cleanup(func() { _ = os.WriteFile(release, nil, 0o600) })

	waitCtx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := first.host.Wait(waitCtx); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled bootstrap waiter error = %v, want context.Canceled", err)
	}
	waitForFile(t, started)
	if err := os.WriteFile(release, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), bootstrapTestTimeout)
	defer cancel()
	if err := first.host.Wait(ctx); err != nil {
		t.Fatalf("wait for admitted bootstrap: %v", err)
	}
	events, err := readEvents(ctx, first.state)
	if err != nil {
		t.Fatal(err)
	}
	if len(events) < 2 || events[len(events)-1].GetResult() == nil || events[len(events)-1].GetResult().ExitCode != 0 {
		t.Fatalf("durable bootstrap events = %v, want output and successful result", events)
	}
	if data, err := os.ReadFile(count); err != nil || string(data) != "b" {
		t.Fatalf("bootstrap script count = %q, %v; want one execution", data, err)
	}
	first.close(t)

	second := newHostTestRuntime(t, &script, stateDir, nil)
	ctx, cancel = context.WithTimeout(context.Background(), bootstrapTestTimeout)
	defer cancel()
	if err := second.host.Wait(ctx); err != nil {
		t.Fatalf("restore completed bootstrap outcome: %v", err)
	}
	if data, err := os.ReadFile(count); err != nil || string(data) != "b" {
		t.Fatalf("bootstrap script count after recovery = %q, %v; want no rerun", data, err)
	}
	bootstrapStatus, found, err := ReadStatus(ctx, second.state)
	if err != nil || !found || bootstrapStatus.Phase != Succeeded {
		t.Fatalf("restored bootstrap status = (%+v, found=%t, err=%v), want succeeded", bootstrapStatus, found, err)
	}
}

func TestConcurrentBootstrapWaitersShareOneAttempt(t *testing.T) {
	stateDir := t.TempDir()
	started := filepath.Join(stateDir, "started")
	release := filepath.Join(stateDir, "release")
	count := filepath.Join(stateDir, "count")
	script := fmt.Sprintf(": > %q\nwhile [ ! -f %q ]; do sleep 0.01; done\nprintf b >> %q\nprintf ready", started, release, count)
	r := newHostTestRuntime(t, &script, stateDir, nil)
	t.Cleanup(func() { _ = os.WriteFile(release, nil, 0o600) })
	initialSignal, found, err := lifecycle.Read[operationSignal](t.Context(), r.state, ResourceID)
	if err != nil || !found || initialSignal.Revision != 0 {
		t.Fatalf("bootstrap operation signal before admission = (%+v, found=%t, err=%v), want initialized revision 0", initialSignal, found, err)
	}

	ctx, cancel := context.WithTimeout(context.Background(), bootstrapTestTimeout)
	defer cancel()
	const waiters = 8
	results := make(chan error, waiters)
	for range waiters {
		go func() { results <- r.host.Wait(ctx) }()
	}
	waitForFile(t, started)
	if err := os.WriteFile(release, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	for range waiters {
		if err := <-results; err != nil {
			t.Fatalf("concurrent bootstrap waiter: %v", err)
		}
	}
	r.workers.Wait()
	if data, err := os.ReadFile(count); err != nil || string(data) != "b" {
		t.Fatalf("bootstrap script count = %q, %v; want one execution", data, err)
	}
	signal, found, err := lifecycle.Read[operationSignal](ctx, r.state, ResourceID)
	if err != nil || !found || signal.Revision != 2 {
		t.Fatalf("bootstrap operation signal = (%+v, found=%t, err=%v), want revision 2", signal, found, err)
	}
}

func TestCompletedBootstrapSignalSurvivesControllerRestart(t *testing.T) {
	stateDir := t.TempDir()
	count := filepath.Join(stateDir, "count")
	script := fmt.Sprintf("printf b >> %q", count)
	r := newHostTestRuntime(t, &script, stateDir, nil)
	release := make(chan struct{})
	var releaseOnce sync.Once
	releasePublication := func() { releaseOnce.Do(func() { close(release) }) }
	t.Cleanup(releasePublication)
	signalState := &operationSignalState{
		State: r.state, revision: 2, publishing: make(chan struct{}), release: release,
	}
	r.host.state = signalState
	if err := r.host.Request(t.Context()); err != nil {
		t.Fatal(err)
	}
	select {
	case <-signalState.publishing:
	case <-time.After(bootstrapTestTimeout):
		t.Fatal("bootstrap completion signal was not published")
	}
	r.stopController(t)
	releasePublication()
	r.workers.Wait()
	if op := r.host.completedOperation(); op == nil || op.Err() != nil {
		t.Fatalf("completed bootstrap slot = %v, want successful result retained", op)
	}

	r.startController(t)
	ctx, cancel := context.WithTimeout(context.Background(), bootstrapTestTimeout)
	defer cancel()
	if err := r.host.Wait(ctx); err != nil {
		t.Fatalf("wait after controller restart: %v", err)
	}
	status, found, err := ReadStatus(ctx, r.state)
	if err != nil || !found || status.Phase != Succeeded {
		t.Fatalf("bootstrap status after controller restart = (%+v, found=%t, err=%v), want succeeded", status, found, err)
	}
	signal, found, err := lifecycle.Read[operationSignal](ctx, r.state, ResourceID)
	if err != nil || !found || signal.Revision != 2 {
		t.Fatalf("bootstrap operation signal after controller restart = (%+v, found=%t, err=%v), want retained revision 2", signal, found, err)
	}
	if data, err := os.ReadFile(count); err != nil || string(data) != "b" {
		t.Fatalf("bootstrap script count after controller restart = %q, %v; want one execution", data, err)
	}
}

func TestCompletionSignalPublicationFailureKeepsBootstrapFailClosed(t *testing.T) {
	stateDir := t.TempDir()
	count := filepath.Join(stateDir, "count")
	script := fmt.Sprintf("printf b >> %q", count)
	r := newHostTestRuntime(t, &script, stateDir, nil)
	publicationErr := errors.New("bootstrap completion signal failed")
	r.host.state = &operationSignalState{State: r.state, revision: 2, err: publicationErr}
	if err := r.host.Request(t.Context()); err != nil {
		t.Fatal(err)
	}
	r.workers.Wait()
	select {
	case err := <-r.failures:
		if !errors.Is(err, publicationErr) {
			t.Fatalf("reported bootstrap error = %v, want signal publication failure", err)
		}
	case <-time.After(bootstrapTestTimeout):
		t.Fatal("completion signal publication failure was not reported")
	}
	status, found, err := ReadStatus(t.Context(), r.state)
	if err != nil || !found || status.Phase != Running {
		t.Fatalf("bootstrap status after completion signal failure = (%+v, found=%t, err=%v), want fail-closed running state", status, found, err)
	}
	op := r.host.completedOperation()
	if op == nil || !errors.Is(op.Err(), publicationErr) {
		t.Fatalf("completed bootstrap result = %v, want publication error retained in operation slot", op)
	}
	signal, found, err := lifecycle.Read[operationSignal](t.Context(), r.state, ResourceID)
	if err != nil || !found || signal.Revision != 1 {
		t.Fatalf("bootstrap operation signal after failed completion publication = (%+v, found=%t, err=%v), want revision 1", signal, found, err)
	}
	r.close(t)

	restarted := newHostTestRuntime(t, &script, stateDir, nil)
	ctx, cancel := context.WithTimeout(context.Background(), bootstrapTestTimeout)
	defer cancel()
	if err := restarted.host.Wait(ctx); err != nil {
		t.Fatalf("recover durable bootstrap outcome after signal publication failure: %v", err)
	}
	if data, err := os.ReadFile(count); err != nil || string(data) != "b" {
		t.Fatalf("bootstrap script count after signal publication recovery = %q, %v; want no replay", data, err)
	}
}

func TestInterruptedAttemptIsReconstructedWithoutRerunning(t *testing.T) {
	stateDir := t.TempDir()
	count := filepath.Join(stateDir, "count")
	script := fmt.Sprintf("printf b >> %q", count)
	store, closer, err := openTestState(stateDir)
	if err != nil {
		t.Fatal(err)
	}
	if err := publishSelection(t.Context(), store, script); err != nil {
		t.Fatal(err)
	}
	log, err := OpenLog(t.Context(), store)
	if err != nil {
		t.Fatal(err)
	}
	if err := log.AppendInterrupted(t.Context(), 1); err != nil {
		t.Fatal(err)
	}
	if err := closer.Close(); err != nil {
		t.Fatal(err)
	}

	r := newHostTestRuntime(t, &script, stateDir, nil)
	ctx, cancel := context.WithTimeout(context.Background(), bootstrapTestTimeout)
	defer cancel()
	if err := r.host.Wait(ctx); !errors.Is(err, ErrInterrupted) {
		t.Fatalf("restored interrupted bootstrap error = %v, want ErrInterrupted", err)
	}
	if _, err := os.Stat(count); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("interrupted bootstrap script ran again: %v", err)
	}
	bootstrapStatus, found, err := ReadStatus(ctx, r.state)
	if err != nil || !found || bootstrapStatus.Phase != Interrupted {
		t.Fatalf("restored interrupted runtime = (%+v, found=%t, err=%v), want interrupted", bootstrapStatus, found, err)
	}
}

func TestSelectionWithoutTerminalEventIsInterruptedAfterRestart(t *testing.T) {
	stateDir := t.TempDir()
	script := fmt.Sprintf("printf ran > %q", filepath.Join(stateDir, "effect"))
	store, closer, err := openTestState(stateDir)
	if err != nil {
		t.Fatal(err)
	}
	if err := publishSelection(t.Context(), store, script); err != nil {
		t.Fatal(err)
	}
	if err := closer.Close(); err != nil {
		t.Fatal(err)
	}

	r := newHostTestRuntime(t, &script, stateDir, nil)
	ctx, cancel := context.WithTimeout(context.Background(), bootstrapTestTimeout)
	defer cancel()
	if err := r.host.Wait(ctx); !errors.Is(err, ErrInterrupted) {
		t.Fatalf("restored selection without terminal event = %v, want interrupted", err)
	}
	if _, err := os.Stat(filepath.Join(stateDir, "effect")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("script ran after restart: %v", err)
	}
}

func TestOutputWithoutTerminalEventIsInterruptedAfterRestart(t *testing.T) {
	stateDir := t.TempDir()
	script := fmt.Sprintf("printf ran > %q", filepath.Join(stateDir, "effect"))
	store, closer, err := openTestState(stateDir)
	if err != nil {
		t.Fatal(err)
	}
	if err := publishSelection(t.Context(), store, script); err != nil {
		t.Fatal(err)
	}
	log, err := OpenLog(t.Context(), store)
	if err != nil {
		t.Fatal(err)
	}
	if err := log.AppendOutput(t.Context(), 1, sessionpb.InitializationStream_INITIALIZATION_STREAM_STDOUT, []byte("partial")); err != nil {
		t.Fatal(err)
	}
	if err := closer.Close(); err != nil {
		t.Fatal(err)
	}

	r := newHostTestRuntime(t, &script, stateDir, nil)
	ctx, cancel := context.WithTimeout(context.Background(), bootstrapTestTimeout)
	defer cancel()
	if err := r.host.Wait(ctx); !errors.Is(err, ErrInterrupted) {
		t.Fatalf("restored output without terminal event = %v, want interrupted", err)
	}
	if _, err := os.Stat(filepath.Join(stateDir, "effect")); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("script ran after partial output recovery: %v", err)
	}
}

func TestOnlyDurableResourcesAreRestored(t *testing.T) {
	stateDir := t.TempDir()
	store, closer, err := openTestState(stateDir)
	if err != nil {
		t.Fatal(err)
	}
	script := string([]byte{0xff, 0x00, 'x'})
	if err := publishSelection(t.Context(), store, script); err != nil {
		t.Fatal(err)
	}
	log, err := OpenLog(t.Context(), store)
	if err != nil {
		t.Fatal(err)
	}
	if err := log.AppendOutput(t.Context(), 1, sessionpb.InitializationStream_INITIALIZATION_STREAM_STDOUT, []byte("retained")); err != nil {
		t.Fatal(err)
	}
	sessions, err := retained.NewStore(filepath.Join(stateDir, "sessions"), store)
	if err != nil {
		t.Fatal(err)
	}
	budget := 1234
	native := "native-1"
	record, err := retained.NewRecord(&sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: "sandbox", Uid: "sandbox-uid"}, Harness: sessionpb.Harness_HARNESS_CLAUDE}}}, Cwd: "/workspace", Model: "model"}, "printf exact", &budget)
	if err != nil {
		t.Fatal(err)
	}
	record.NativeSessionId = &native
	record.ExecutionIdentity = &identitypb.Identity{Name: "identity", Uid: "uid"}
	if err := sessions.Write("session-1", record); err != nil {
		t.Fatal(err)
	}
	record.InitialModel = "new-model"
	if err := sessions.Write("session-1", record); err != nil {
		t.Fatal(err)
	}
	ack := retained.Acknowledgement{Cursor: 43, Sealed: true}
	if err := sessions.WriteAcknowledgement("session-1", ack); err != nil {
		t.Fatal(err)
	}
	metadata := []struct {
		pointer resource.Metadata
		owner   string
	}{
		{*SelectionResourceValue("").Metadata(), HostOwner},
		{*EventResourceValue(&sessionpb.InitializationEvent{Sequence: 1, Attempt: 1, Observation: &sessionpb.InitializationEvent_Output{Output: &sessionpb.InitializationOutput{Stream: sessionpb.InitializationStream_INITIALIZATION_STREAM_STDOUT, Data: []byte("retained")}}}).Metadata(), HostOwner},
		{*retained.SessionResource("session-1", &retained.Record{}).Metadata(), lifecycle.EnvironmentOwner},
		{*retained.AcknowledgementResource("session-1", retained.Acknowledgement{}).Metadata(), lifecycle.EnvironmentOwner},
	}
	versions := make([]resource.Version, len(metadata))
	for i, item := range metadata {
		current, err := store.Get(t.Context(), &item.pointer)
		if err != nil {
			t.Fatal(err)
		}
		updated := current.DeepCopy()
		updated.Metadata().Finalizers().Add("retention-proof")
		if err := store.Update(t.Context(), updated, state.WithUpdateOwner(item.owner)); err != nil {
			t.Fatal(err)
		}
		versions[i] = updated.Metadata().Version()
	}
	if err := lifecycle.Publish(t.Context(), store, ResourceID, Request{Requested: true}, HostOwner); err != nil {
		t.Fatal(err)
	}
	if err := lifecycle.Publish(t.Context(), store, ResourceID, Status{Phase: Running}, HostOwner); err != nil {
		t.Fatal(err)
	}
	if err := lifecycle.Publish(t.Context(), store, ResourceID, operationSignal{Revision: 2}, HostOwner); err != nil {
		t.Fatal(err)
	}
	if err := closer.Close(); err != nil {
		t.Fatal(err)
	}
	restored, restoredCloser, err := openTestState(stateDir)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = restoredCloser.Close() })
	if _, found, err := lifecycle.Read[operationSignal](t.Context(), restored, ResourceID); err != nil || found {
		t.Fatalf("restored bootstrap operation signal = (%t, %v), want live-only signal absent", found, err)
	}
	for i, item := range metadata {
		value, err := restored.Get(t.Context(), &item.pointer)
		if err != nil {
			t.Fatal(err)
		}
		if value.Metadata().Owner() != item.owner || !value.Metadata().Version().Equal(versions[i]) || !value.Metadata().Finalizers().Has("retention-proof") {
			t.Fatalf("restored %s metadata = %v, want owner=%s version=%s finalizer", item.pointer.Type(), value.Metadata(), item.owner, versions[i])
		}
	}
	restoredSessions, err := retained.NewStore(filepath.Join(stateDir, "sessions"), restored)
	if err != nil {
		t.Fatal(err)
	}
	gotAck, err := restoredSessions.ReadAcknowledgement("session-1")
	if err != nil || gotAck == nil || *gotAck != ack {
		t.Fatalf("restored acknowledgement = %+v, %v", gotAck, err)
	}
	ids, err := restoredSessions.SessionIDs()
	if err != nil || len(ids) != 1 || ids[0] != "session-1" {
		t.Fatalf("restored Session IDs = %v, %v", ids, err)
	}
	restoredRecord, err := restoredSessions.Read("session-1")
	if err != nil || restoredRecord.InitialModel != "new-model" || !bytes.Equal(restoredRecord.SetupScript, record.SetupScript) || restoredRecord.EventSourceId != record.EventSourceId || restoredRecord.NativeSessionId == nil || *restoredRecord.NativeSessionId != native || restoredRecord.TotalContextBudgetTokens == nil || *restoredRecord.TotalContextBudgetTokens != int64(budget) || restoredRecord.ExecutionIdentity.GetName() != "identity" || restoredRecord.ExecutionIdentity.GetUid() != "uid" {
		t.Fatalf("restored Session record = %+v, %v", restoredRecord, err)
	}
	restoredRecord.InitialModel = "after-restart"
	if err := restoredSessions.Write("session-1", restoredRecord); err != nil {
		t.Fatal(err)
	}
	if updated, err := restoredSessions.Read("session-1"); err != nil || updated.InitialModel != "after-restart" {
		t.Fatalf("updated restored Session = %+v, %v", updated, err)
	}
	selection, found, err := ReadSelection(t.Context(), restored)
	if err != nil || !found || selection != script {
		t.Fatalf("restored selection = (%+v, %t, %v)", selection, found, err)
	}
	if _, found, err := ReadRequest(t.Context(), restored); err != nil || found {
		t.Fatalf("restored admission = (%t, %v), want absent", found, err)
	}
	if _, found, err := ReadStatus(t.Context(), restored); err != nil || found {
		t.Fatalf("restored bootstrap status = (%t, %v), want absent", found, err)
	}
	if err := restoredCloser.Close(); err != nil {
		t.Fatal(err)
	}
	again, againCloser, err := openTestState(stateDir)
	if err != nil {
		t.Fatal(err)
	}
	defer againCloser.Close()
	againSessions, err := retained.NewStore(filepath.Join(stateDir, "sessions"), again)
	if err != nil {
		t.Fatal(err)
	}
	if updated, err := againSessions.Read("session-1"); err != nil || updated.InitialModel != "after-restart" {
		t.Fatalf("reloaded Session after update = %+v, %v", updated, err)
	}
}

func TestDifferingSelectedScriptIsRejectedAfterRestart(t *testing.T) {
	stateDir := t.TempDir()
	store, closer, err := openTestState(stateDir)
	if err != nil {
		t.Fatal(err)
	}
	if err := publishSelection(t.Context(), store, "printf one"); err != nil {
		t.Fatal(err)
	}
	if err := closer.Close(); err != nil {
		t.Fatal(err)
	}
	different := "printf two"
	r := newHostTestRuntime(t, &different, stateDir, nil)
	ctx, cancel := context.WithTimeout(context.Background(), bootstrapTestTimeout)
	defer cancel()
	if err := r.host.Wait(ctx); status.Code(err) != codes.AlreadyExists {
		t.Fatalf("different selected script = %v, want AlreadyExists", err)
	}
}

func TestRequestDetectsAdmissionGuardThatSkippedEffect(t *testing.T) {
	stateDir := t.TempDir()
	script := "true"
	r := newHostTestRuntime(t, &script, stateDir, func(context.Context, lifecycle.EnvironmentDesired, func(context.Context) error) error {
		return nil
	})
	if err := r.host.Request(r.ctx); err == nil {
		t.Fatal("request succeeded although the admission guard skipped its effect")
	} else if code := status.Code(err); code != codes.Aborted {
		t.Fatalf("skipped admission error code = %s, want Aborted: %v", code, err)
	}
	input, found, err := ReadRequest(r.ctx, r.state)
	if err != nil || !found || input.Requested {
		t.Fatalf("input after skipped admission = (%+v, found=%t, err=%v), want unrequested", input, found, err)
	}
	if err := lifecycle.Publish(r.ctx, r.state, ResourceID, Request{Requested: true}, HostOwner); err != nil {
		t.Fatal(err)
	}
	if err := lifecycle.PublishEnvironmentDesired(r.ctx, r.state, lifecycle.EnvironmentDesired{Mode: lifecycle.Closing, Generation: 2}); err != nil {
		t.Fatal(err)
	}
	if err := r.host.Request(r.ctx); err != nil {
		t.Fatalf("repeat of already accepted request during Close: %v", err)
	}
}

func TestSelectionByteLimit(t *testing.T) {
	maximum := strings.Repeat("x", maximumBootstrapBytes)
	if err := validateScript(maximum); err != nil {
		t.Fatalf("maximum selected bytes rejected: %v", err)
	}
	if err := validateScript(maximum + "x"); err == nil {
		t.Fatal("oversize bootstrap selection accepted")
	}
}

func waitForFile(t *testing.T, path string) {
	t.Helper()
	deadline := time.Now().Add(bootstrapTestTimeout)
	for time.Now().Before(deadline) {
		if _, err := os.Stat(path); err == nil {
			return
		}
		time.Sleep(10 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", path)
}
