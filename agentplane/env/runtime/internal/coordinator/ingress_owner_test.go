package coordinator

import (
	"context"
	"errors"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/bootstrap"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/cosistate"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/internal/retained"
	ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/api/ingress/session_ingresspb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
)

type pausedRecoveryState struct {
	state.State
	once       sync.Once
	entered    chan struct{}
	release    chan struct{}
	err        error
	allowReads atomic.Bool
}

type failingSealedAcknowledgementState struct {
	state.State
	err      error
	failed   chan struct{}
	failOnce sync.Once
}

func (s *failingSealedAcknowledgementState) Modify(ctx context.Context, value resource.Resource, update func(resource.Resource) error, opts ...state.UpdateOption) error {
	return s.State.Modify(ctx, value, func(current resource.Resource) error {
		if err := update(current); err != nil {
			return err
		}
		if ack, ok := current.(*retained.IngressAckResource); ok && ack.TypedSpec().Value.Sealed {
			s.failOnce.Do(func() { close(s.failed) })
			return s.err
		}
		return nil
	}, opts...)
}

func (s *pausedRecoveryState) List(ctx context.Context, kind resource.Kind, opts ...state.ListOption) (resource.List, error) {
	if kind.Type() == retained.SessionRecordType && !s.allowReads.Load() {
		s.once.Do(func() { close(s.entered) })
		select {
		case <-s.release:
		case <-ctx.Done():
			return resource.List{}, ctx.Err()
		}
		if s.err != nil {
			return resource.List{}, s.err
		}
	}
	return s.State.List(ctx, kind, opts...)
}

func ingressTestRuntime(t *testing.T, store state.State, deps runtimeDeps) *Runtime {
	t.Helper()
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false")
	config.Config.HistoryIngress.Target = "history.test:8081"
	r, err := newTestRuntime(t, config.Config, store, deps)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.startSessions(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
		defer cancel()
		_ = r.Close(ctx)
		if ctx.Err() != nil {
			r.lifecycleCancel()
			<-r.lifecycleDone
		}
	})
	return r
}

func ingressTestContext(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(t.Context(), lifecycleTestTimeout)
	t.Cleanup(cancel)
	return ctx
}

func TestRuntimeHistoryStartsAfterSessionRecovery(t *testing.T) {
	store := &pausedRecoveryState{
		State: state.WrapCore(namespaced.NewState(inmem.Build)), entered: make(chan struct{}), release: make(chan struct{}),
	}
	started := make(chan struct{})
	r := ingressTestRuntime(t, store, runtimeDeps{runIngress: func(ctx context.Context, _ configuration.Endpoint) error {
		close(started)
		<-ctx.Done()
		return nil
	}})
	t.Cleanup(func() {
		select {
		case <-store.release:
		default:
			close(store.release)
		}
	})
	ctx := ingressTestContext(t)
	select {
	case <-store.entered:
	case <-ctx.Done():
		t.Fatal("Session recovery did not reach retained inventory")
	}
	select {
	case <-started:
		t.Fatal("History delivery started before Session recovery")
	default:
	}
	close(store.release)
	if err := r.waitForInitialSessions(ctx); err != nil {
		t.Fatal(err)
	}
	select {
	case <-started:
	case <-ctx.Done():
		t.Fatal("History delivery did not start after recovery")
	}
	if err := r.Close(ctx); err != nil {
		t.Fatalf("Runtime Close: %v", err)
	}
	select {
	case <-r.ingress.done:
	default:
		t.Fatal("Runtime released owner before joining History delivery")
	}
}

func TestRuntimeRecoveryFailureNeverStartsHistoryDelivery(t *testing.T) {
	recoveryErr := errors.New("injected Session recovery failure")
	store := &pausedRecoveryState{
		State: state.WrapCore(namespaced.NewState(inmem.Build)), entered: make(chan struct{}), release: make(chan struct{}), err: recoveryErr,
	}
	started := make(chan struct{})
	stopping := make(chan struct{})
	r := ingressTestRuntime(t, store, runtimeDeps{
		runIngress: func(context.Context, configuration.Endpoint) error {
			close(started)
			return nil
		},
		stopSessions: func(context.Context) error {
			close(stopping)
			return nil
		},
	})
	t.Cleanup(func() {
		select {
		case <-store.release:
		default:
			close(store.release)
		}
	})
	ctx := ingressTestContext(t)
	select {
	case <-store.entered:
	case <-ctx.Done():
		t.Fatal("Session recovery did not start")
	}
	close(store.release)
	if err := r.waitForInitialSessions(ctx); !errors.Is(err, recoveryErr) {
		t.Fatalf("initial Host wait = %v, want recovery failure", err)
	}
	waitRuntime(t, r, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.failed
	}, "Runtime to observe failed Session recovery")
	if err := r.waitForInitialSessions(ctx); !errors.Is(err, recoveryErr) {
		t.Fatalf("initial Host wait after Runtime failure = %v, want recovery failure", err)
	}
	select {
	case <-r.ingress.done:
	case <-ctx.Done():
		t.Fatal("History worker did not settle after recovery failure")
	}
	select {
	case <-started:
		t.Fatal("History delivery started after failed Session recovery")
	default:
	}
	// Let the aggregate read inventory during shutdown after the recovery
	// worker's read has already failed.
	store.allowReads.Store(true)
	if err := r.Close(ctx); !errors.Is(err, recoveryErr) {
		t.Fatalf("Runtime Close after failed recovery = %v, want recovery failure", err)
	}
	select {
	case <-stopping:
	default:
		t.Fatal("Runtime skipped Session cleanup after recovery failure")
	}
}

func TestRuntimeHistoryFailureFencesAdmissionAndCloses(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	ingressErr := errors.New("injected History delivery failure")
	r := ingressTestRuntime(t, nil, runtimeDeps{runIngress: func(context.Context, configuration.Endpoint) error {
		close(started)
		<-release
		return ingressErr
	}})
	t.Cleanup(func() {
		select {
		case <-release:
		default:
			close(release)
		}
	})
	ctx := ingressTestContext(t)
	if err := r.waitForInitialSessions(ctx); err != nil {
		t.Fatal(err)
	}
	select {
	case <-started:
	case <-ctx.Done():
		t.Fatal("History worker did not start")
	}
	close(release)
	waitRuntime(t, r, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.failed
	}, "fatal History failure")
	r.mu.Lock()
	admissionOpen := r.admissionOpen
	r.mu.Unlock()
	if admissionOpen {
		t.Fatal("Runtime still admits calls after History delivery failure")
	}
	if err := r.Close(ctx); !errors.Is(err, ingressErr) {
		t.Fatalf("Runtime Close after History failure = %v", err)
	}
}

func TestRuntimeCloseJoinsHistoryBeforeReleasingOwner(t *testing.T) {
	started := make(chan struct{})
	canceled := make(chan struct{})
	release := make(chan struct{})
	r := ingressTestRuntime(t, nil, runtimeDeps{runIngress: func(ctx context.Context, _ configuration.Endpoint) error {
		close(started)
		<-ctx.Done()
		close(canceled)
		<-release
		return nil
	}})
	t.Cleanup(func() {
		select {
		case <-release:
		default:
			close(release)
		}
	})
	ctx := ingressTestContext(t)
	if err := r.waitForInitialSessions(ctx); err != nil {
		t.Fatal(err)
	}
	select {
	case <-started:
	case <-ctx.Done():
		t.Fatal("History worker did not start")
	}
	closed := make(chan error, 1)
	go func() { closed <- r.Close(ctx) }()
	select {
	case <-canceled:
	case <-ctx.Done():
		t.Fatal("Runtime did not cancel History delivery after Host drain")
	}
	if owner, err := openTestRuntimeStore(r.config.StateDir); err == nil {
		_ = owner.Close()
		t.Fatal("Runtime released owner before History worker join")
	}
	select {
	case err := <-closed:
		t.Fatalf("Runtime Close completed before History worker joined: %v", err)
	default:
	}
	close(release)
	select {
	case err := <-closed:
		if err != nil {
			t.Fatalf("Runtime Close: %v", err)
		}
	case <-ctx.Done():
		t.Fatal("Runtime Close did not join History worker")
	}
	owner, err := openTestRuntimeStore(r.config.StateDir)
	if err != nil {
		t.Fatalf("joined Runtime retained owner: %v", err)
	}
	_ = owner.Close()
}

const sealedHistorySessionID = "session-1"

type heldHistorySeal struct {
	received <-chan struct{}
	release  func()
}

func sealedHistoryConfig(t *testing.T) (runtimeTestConfig, heldHistorySeal) {
	t.Helper()
	sealSeen := make(chan struct{})
	releaseSeal := make(chan struct{})
	history := &acknowledgingHistory{
		acks: map[string]*ingresspb.Acknowledgement{}, sealSeen: sealSeen, releaseSeal: releaseSeal,
	}
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, t.TempDir(), nil, "/bin/false")
	config.Config.HistoryIngress.Target = startHistoryService(t, history)
	return config, heldHistorySeal{received: sealSeen, release: sync.OnceFunc(func() { close(releaseSeal) })}
}

func (seal heldHistorySeal) startRuntimeClose(t *testing.T, r *Runtime) (context.Context, <-chan error) {
	t.Helper()
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	session := openLifecycleSession(sealedHistorySessionID, filepath.Join(r.config.WorkspaceRoot, "project"))
	if _, err := client.harnessHost.CreateSession(client.callCtx, session); err != nil {
		t.Fatal(err)
	}
	ctx := ingressTestContext(t)
	if _, err := r.Quiesce(ctx, &sessionpb.QuiesceRequest{}); err != nil {
		t.Fatal(err)
	}
	select {
	case <-seal.received:
	case <-ctx.Done():
		t.Fatal("History did not receive the Session seal")
	}
	closed := make(chan error, 1)
	go func() { closed <- r.Close(ctx) }()
	waitClosingSessionStage(t, r, lifecycle.Flushing)
	return ctx, closed
}

type closeHistoryReadProbe struct {
	state.State
	read chan struct{}
	once sync.Once
}

func (s *closeHistoryReadProbe) Get(ctx context.Context, pointer resource.Pointer, options ...state.GetOption) (resource.Resource, error) {
	value, err := s.State.Get(ctx, pointer, options...)
	// The sealed History wait reads aggregate status on the fence deadline.
	// Native-stop observation and the aggregate writer use the engine lifetime.
	if _, bounded := ctx.Deadline(); bounded && err == nil && pointer.Type() == lifecycle.AggregateStatusType {
		status := value.(interface{ TypedSpec() *lifecycle.Status }).TypedSpec()
		desired, found, err := lifecycle.ReadEnvironmentDesired(ctx, s.State)
		if err == nil && found && desired.Mode == lifecycle.Closing && status.Generation == desired.Generation && status.Stage == lifecycle.Flushing {
			s.once.Do(func() { close(s.read) })
		}
	}
	return value, err
}

func newSealedHistoryCloseProbe(t *testing.T, config runtimeTestConfig) (*Runtime, <-chan struct{}) {
	t.Helper()
	store, backing, err := cosistate.Open(config.StateDir, lifecycle.Namespace, bootstrap.SelectionRegistration(), bootstrap.EventRegistration(), retained.SessionRecordRegistration(), retained.IngressAckRegistration(), cosistate.EnvironmentSealRegistration())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = backing.Close() })
	config.testBacking = backing
	probe := &closeHistoryReadProbe{State: store, read: make(chan struct{})}
	return newLifecycleRuntimeWithState(t, config, probe), probe.read
}

func TestRuntimeKeepsHistoryAliveUntilExactSealedAcknowledgement(t *testing.T) {
	config, seal := sealedHistoryConfig(t)
	r, closeRead := newSealedHistoryCloseProbe(t, config)
	t.Cleanup(seal.release)
	ctx, closed := seal.startRuntimeClose(t, r)
	waitSignal(t, closeRead, "Close did not observe the unacknowledged seal")
	select {
	case <-r.ingress.done:
		t.Fatal("Runtime stopped History delivery before the sealed acknowledgement")
	case err := <-closed:
		t.Fatalf("Runtime Close accepted an unacknowledged seal: %v", err)
	default:
	}
	if owner, err := openTestRuntimeStore(r.config.StateDir); err == nil {
		_ = owner.Close()
		t.Fatal("Runtime released owner before sealed History acknowledgement")
	}
	seal.release()
	select {
	case err := <-closed:
		if err != nil {
			t.Fatalf("Runtime Close after exact History acknowledgement: %v", err)
		}
	case <-ctx.Done():
		t.Fatal("Runtime Close did not finish after the exact History acknowledgement")
	}
}

func TestLateSealedAcknowledgementAfterEngineStopKeepsRuntimeOwner(t *testing.T) {
	config, seal := sealedHistoryConfig(t)
	config.expectCloseError = true
	var r *Runtime
	t.Cleanup(func() {
		if r != nil {
			_ = closeTestRuntimeStore(r)
		}
	})
	r, closeRead := newSealedHistoryCloseProbe(t, config)
	t.Cleanup(seal.release)
	ctx, closed := seal.startRuntimeClose(t, r)
	waitSignal(t, closeRead, "Close did not observe the unacknowledged seal")
	journalSeal := r.sessions.Get(sealedHistorySessionID).EventJournal().SealState()
	if journalSeal == nil {
		t.Fatal("History received the seal before the journal committed it")
	}

	r.lifecycleCancel()
	select {
	case <-r.lifecycleDone:
	case <-ctx.Done():
		t.Fatal("lifecycle engine did not join while History was waiting for the acknowledgement")
	}
	if err := waitClose(t, closed); err == nil || errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("Close after engine stop = %v, want lifecycle failure", err)
	}

	seal.release()
	ack := retained.AcknowledgementResource(sealedHistorySessionID, retained.Acknowledgement{})
	if _, err := r.lifecycleState.WatchFor(ctx, ack.Metadata(), state.WithCondition(func(value resource.Resource) (bool, error) {
		ack, ok := value.(*retained.IngressAckResource)
		return ok && ack.TypedSpec().Value.Sealed && ack.TypedSpec().Value.Cursor == journalSeal.Cursor, nil
	})); err != nil {
		t.Fatalf("wait for late durable sealed acknowledgement: %v", err)
	}
	if owner, err := openTestRuntimeStore(r.config.StateDir); err == nil {
		_ = owner.Close()
		t.Fatal("late acknowledgement released the owner after the lifecycle engine stopped")
	}
}

func TestRuntimeRetainsOwnerWhenHistoryFailsBeforeSealedAcknowledgement(t *testing.T) {
	failDelivery := make(chan struct{})
	fail := sync.OnceFunc(func() { close(failDelivery) })
	deliveryError := errors.New("injected local History delivery failure")
	// Run after the lifecycle fixture's cleanup, which stops delivery and Session workers.
	var r *Runtime
	runtimeReady := make(chan struct{})
	t.Cleanup(func() {
		if r != nil {
			_ = closeTestRuntimeStore(r)
		}
	})
	config, seal := sealedHistoryConfig(t)
	config.expectCloseError = true
	config.deps.runIngress = func(ctx context.Context, endpoint configuration.Endpoint) error {
		select {
		case <-runtimeReady:
		case <-ctx.Done():
			return nil
		}
		deliveryCtx, cancel := context.WithCancel(ctx)
		deliveryDone := make(chan error, 1)
		go func() {
			supervisor, err := r.ingressSupervisor(endpoint, r.fail)
			if err != nil {
				deliveryDone <- err
				return
			}
			deliveryDone <- supervisor.Run(deliveryCtx)
		}()
		select {
		case <-failDelivery:
			cancel()
			<-deliveryDone
			return deliveryError
		case err := <-deliveryDone:
			cancel()
			return err
		case <-ctx.Done():
			cancel()
			<-deliveryDone
			return nil
		}
	}
	r = newLifecycleRuntime(t, config)
	close(runtimeReady)
	t.Cleanup(func() {
		fail()
		cleanupCtx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
		defer cancel()
		select {
		case <-r.ingress.done:
		case <-cleanupCtx.Done():
			t.Error("History delivery worker did not join during test cleanup")
		}
		seal.release()
	})
	ctx, closed := seal.startRuntimeClose(t, r)
	ack, found, err := retained.ReadAcknowledgement(ctx, r.lifecycleState, sealedHistorySessionID)
	if err != nil || found && ack.Sealed {
		t.Fatalf("durable sealed History ACK before failure = %+v, found=%t, err=%v", ack, found, err)
	}
	fail()
	select {
	case <-r.ingress.done:
	case <-ctx.Done():
		t.Fatal("local History delivery failure did not join its worker")
	}
	ack, found, err = retained.ReadAcknowledgement(ctx, r.lifecycleState, sealedHistorySessionID)
	if err != nil || found && ack.Sealed {
		t.Fatalf("durable sealed History ACK after failed delivery = %+v, found=%t, err=%v", ack, found, err)
	}
	waitRuntime(t, r, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.failed
	}, "Runtime to observe History delivery failure")
	select {
	case err := <-closed:
		if err == nil || !strings.Contains(err.Error(), deliveryError.Error()) {
			t.Fatalf("Runtime Close after failed sealed History delivery = %v, want delivery failure", err)
		}
	case <-ctx.Done():
		t.Fatal("Runtime Close did not report the failed sealed History delivery")
	}
	if owner, err := openTestRuntimeStore(r.config.StateDir); err == nil {
		_ = owner.Close()
		t.Fatal("Runtime released owner without the exact durable sealed History acknowledgement")
	}
}

func TestRuntimeRetainsOwnerWhenSessionDeliveryCannotPersistSealedAcknowledgement(t *testing.T) {
	storageErr := errors.New("injected sealed History acknowledgement storage failure")
	store := &failingSealedAcknowledgementState{
		State: state.WrapCore(namespaced.NewState(inmem.Build)), err: storageErr, failed: make(chan struct{}),
	}
	var r *Runtime
	t.Cleanup(func() {
		if r != nil {
			_ = closeTestRuntimeStore(r)
		}
	})
	config, seal := sealedHistoryConfig(t)
	config.expectCloseError = true
	r = newLifecycleRuntimeWithState(t, config, store)
	t.Cleanup(seal.release)
	ctx, closed := seal.startRuntimeClose(t, r)
	seal.release()
	select {
	case <-store.failed:
	case <-ctx.Done():
		t.Fatal("sealed History acknowledgement write did not fail")
	}
	select {
	case err := <-closed:
		if err == nil || !strings.Contains(err.Error(), storageErr.Error()) {
			t.Fatalf("Runtime Close after local sealed ACK storage failure = %v, want storage cause", err)
		}
	case <-ctx.Done():
		t.Fatal("Runtime Close did not report local sealed ACK storage failure")
	}
	ack, found, err := retained.ReadAcknowledgement(ctx, r.lifecycleState, sealedHistorySessionID)
	if err != nil || found && ack.Sealed {
		t.Fatalf("durable sealed History ACK after failed write = %+v, found=%t, err=%v", ack, found, err)
	}
	if owner, err := openTestRuntimeStore(r.config.StateDir); err == nil {
		_ = owner.Close()
		t.Fatal("Runtime released owner without durable sealed History acknowledgement")
	}
}
