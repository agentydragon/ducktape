package runtime

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"syscall"
	"testing"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/session"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type aggregateProbeRuntime struct {
	controller.Runtime
	storage state.State
	onList  func()
}

func (p aggregateProbeRuntime) Get(ctx context.Context, ptr resource.Pointer, opts ...state.GetOption) (resource.Resource, error) {
	return p.storage.Get(ctx, ptr, opts...)
}

func (p aggregateProbeRuntime) List(ctx context.Context, kind resource.Kind, opts ...state.ListOption) (resource.List, error) {
	if p.onList != nil {
		p.onList()
	}
	return p.storage.List(ctx, kind, opts...)
}

func (p aggregateProbeRuntime) Modify(ctx context.Context, value resource.Resource, update func(resource.Resource) error, _ ...controller.ModifyOption) error {
	return p.storage.Modify(ctx, value, update, state.WithUpdateOwner(sessionLifecycleControllerName))
}

func (aggregateProbeRuntime) QueueReconcile() {}

func TestAggregateRequiresCurrentSessionPreFenceSettlement(t *testing.T) {
	workspace := t.TempDir()
	config := lifecycleConfig(t, modelConfigServer(t).URL, workspace, nil, "/bin/false")
	config.expectCloseError = true
	r := newSessionTestRuntime(t, config)
	if _, err := r.CreateSession(sessionCallContext(r, t.Context()), openLifecycleSession("pre-fence", filepath.Join(workspace, "project"))); err != nil {
		t.Fatal(err)
	}
	waitLifecycle(t, r, func() bool {
		_, found, err := session.ReadLifecycleStatus(r.lifecycleContext, r.lifecycleState, "pre-fence")
		return err == nil && found
	}, "initial Session lifecycle status")
	ctx := t.Context()
	statusPtr := resource.NewMetadata(lifecycle.Namespace, session.LifecycleStatusType, "pre-fence", resource.VersionUndefined)
	ownerPtr := resource.NewMetadata(lifecycle.Namespace, session.OwnerStatusType, "pre-fence", resource.VersionUndefined)
	aggregatePtr := resource.NewMetadata(lifecycle.Namespace, lifecycle.AggregateStatusType, lifecycle.ResourceID, resource.VersionUndefined)
	var outputs []resource.Resource
	for _, ptr := range []resource.Pointer{statusPtr, ownerPtr, aggregatePtr} {
		value, err := r.lifecycleState.Get(ctx, ptr)
		if err != nil {
			t.Fatal(err)
		}
		outputs = append(outputs, value)
	}
	r.lifecycleCancel()
	<-r.lifecycleDone
	// COSI removes exclusive controller outputs on engine exit. Restore the
	// observed resources for a single-threaded aggregate probe.
	for _, value := range outputs {
		if _, err := r.lifecycleState.Get(ctx, value.Metadata()); state.IsNotFoundError(err) {
			copy := value.DeepCopy()
			copy.Metadata().SetVersion(resource.VersionUndefined)
			if err := r.lifecycleState.Create(ctx, copy, state.WithCreateOwner(string(value.Metadata().Owner()))); err != nil {
				t.Fatal(err)
			}
		} else if err != nil {
			t.Fatal(err)
		}
	}
	desired := lifecycle.EnvironmentDesired{Mode: lifecycle.Quiescing, Generation: 7}
	if err := lifecycle.PublishEnvironmentDesired(ctx, r.lifecycleState, desired); err != nil {
		t.Fatal(err)
	}
	r.mu.Lock()
	r.desired = desired
	r.mu.Unlock()
	saved := outputs[0]
	setSessionStatus := func(generation uint64, settled bool) {
		t.Helper()
		_, err := r.lifecycleState.UpdateWithConflicts(ctx, statusPtr, func(value resource.Resource) error {
			current := value.(interface {
				TypedSpec() *session.LifecycleStatus
			}).TypedSpec()
			*current = session.LifecycleStatus{EnvironmentGeneration: generation, Phase: session.Halted, ProcessStopped: true, PreFenceSettled: settled}
			return nil
		}, state.WithUpdateOwner(session.ControllerName))
		if err != nil {
			t.Fatal(err)
		}
	}
	resetAggregate := func() {
		t.Helper()
		_, err := r.lifecycleState.UpdateWithConflicts(ctx, aggregatePtr, func(value resource.Resource) error {
			*value.(interface{ TypedSpec() *lifecycle.Status }).TypedSpec() = lifecycle.Status{Generation: 7, Stage: lifecycle.Stopping}
			return nil
		}, state.WithUpdateOwner(sessionLifecycleControllerName))
		if err != nil {
			t.Fatal(err)
		}
	}
	probe := aggregateProbeRuntime{storage: r.lifecycleState}
	controller := &sessionLifecycleController{runtime: r}
	for _, test := range []struct {
		name       string
		present    bool
		generation uint64
		settled    bool
		wantNative bool
		wantBlock  lifecycle.BlockerKind
	}{
		{"missing", false, 0, false, false, lifecycle.SessionHaltPending},
		{"stale generation", true, 6, true, false, lifecycle.SessionHaltPending},
		{"pending inspection", true, 7, false, false, lifecycle.SessionInspectionPending},
		{"settled", true, 7, true, true, lifecycle.NoBlocker},
	} {
		t.Run(test.name, func(t *testing.T) {
			if test.present {
				if _, err := r.lifecycleState.Get(ctx, statusPtr); state.IsNotFoundError(err) {
					if err := r.lifecycleState.Create(ctx, saved.DeepCopy(), state.WithCreateOwner(session.ControllerName)); err != nil {
						t.Fatal(err)
					}
				}
				setSessionStatus(test.generation, test.settled)
			} else if err := r.lifecycleState.Destroy(ctx, statusPtr, state.WithDestroyOwner(session.ControllerName)); err != nil {
				t.Fatal(err)
			}
			resetAggregate()
			if err := controller.reconcile(ctx, probe); err != nil {
				t.Fatal(err)
			}
			got, _, err := lifecycle.ReadStatus(ctx, r.lifecycleState)
			if err != nil {
				t.Fatal(err)
			}
			if got.NativeStopped != test.wantNative || got.Blocker.Kind != test.wantBlock {
				t.Fatalf("aggregate = %+v, want NativeStopped=%t blocker=%v", got, test.wantNative, test.wantBlock)
			}
		})
	}
	t.Run("settles during reconciliation", func(t *testing.T) {
		setSessionStatus(7, false)
		resetAggregate()
		// Settle after the first status read. This pass must use its original
		// observation; the LifecycleStatus update wakes the next pass.
		probe.onList = func() { setSessionStatus(7, true) }
		if err := controller.reconcile(ctx, probe); err != nil {
			t.Fatal(err)
		}
		got, _, err := lifecycle.ReadStatus(ctx, r.lifecycleState)
		if err != nil || got.NativeStopped || got.Blocker.Kind != lifecycle.SessionInspectionPending {
			t.Fatalf("aggregate before next pass = %+v, err = %v", got, err)
		}
		probe.onList = nil
		if err := controller.reconcile(ctx, probe); err != nil {
			t.Fatal(err)
		}
		got, _, err = lifecycle.ReadStatus(ctx, r.lifecycleState)
		if err != nil || !got.NativeStopped {
			t.Fatalf("aggregate after next pass = %+v, err = %v", got, err)
		}
	})
}

type admissionStatusReadFailure struct {
	context context.Context
	err     error
}

type harnessHostAdmissionProbeState struct {
	state.State
	statusReadMu           sync.Mutex
	statusReadFailures     []admissionStatusReadFailure
	failDesiredPublication atomic.Bool
	desiredPublicationErr  error
	dropAdmissionUpdate    atomic.Bool
}

func (s *harnessHostAdmissionProbeState) failNextStatusRead(ctx context.Context, err error) {
	s.statusReadMu.Lock()
	defer s.statusReadMu.Unlock()
	s.statusReadFailures = append(s.statusReadFailures, admissionStatusReadFailure{context: ctx, err: err})
}

func (s *harnessHostAdmissionProbeState) Get(ctx context.Context, pointer resource.Pointer, options ...state.GetOption) (resource.Resource, error) {
	if pointer.Type() == lifecycle.AggregateStatusType {
		s.statusReadMu.Lock()
		for index, failure := range s.statusReadFailures {
			if ctx == failure.context {
				s.statusReadFailures = append(s.statusReadFailures[:index], s.statusReadFailures[index+1:]...)
				s.statusReadMu.Unlock()
				return nil, failure.err
			}
		}
		s.statusReadMu.Unlock()
	}
	return s.State.Get(ctx, pointer, options...)
}

func (s *harnessHostAdmissionProbeState) Modify(ctx context.Context, target resource.Resource, update func(resource.Resource) error, options ...state.UpdateOption) error {
	if target.Metadata().Type() == lifecycle.EnvironmentDesiredStateType && s.failDesiredPublication.Swap(false) {
		return s.desiredPublicationErr
	}
	return s.State.Modify(ctx, target, update, options...)
}

func (s *harnessHostAdmissionProbeState) UpdateWithConflicts(ctx context.Context, pointer resource.Pointer, update state.UpdaterFunc, options ...state.UpdateOption) (resource.Resource, error) {
	if pointer.Type() == lifecycle.AdmissionCountType && s.dropAdmissionUpdate.Swap(false) {
		// Admission holds Runtime.mu through Update, so WatchState cannot see
		// the missing count and revoke its epoch before accounting fails.
		if err := s.State.Destroy(ctx, pointer, state.WithDestroyOwner(lifecycle.EnvironmentOwner)); err != nil {
			return nil, err
		}
	}
	return s.State.UpdateWithConflicts(ctx, pointer, update, options...)
}

func lifecycleTestSession(t *testing.T) (*Runtime, *session.Session) {
	t.Helper()
	workspace := t.TempDir()
	proxy := modelConfigServer(t)
	r := newSessionTestRuntime(t, lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"))
	if _, err := r.CreateSession(sessionCallContext(r, context.Background()), openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
		t.Fatal(err)
	}
	return r, r.sessions.Get("session-1")
}

func lifecycleTestBlockedSetup(t *testing.T) (*Runtime, *session.Session, func()) {
	t.Helper()
	return lifecycleTestBlockedSetupWithState(t, nil)
}

func lifecycleTestBlockedSetupWithState(t *testing.T, store state.State) (*Runtime, *session.Session, func()) {
	t.Helper()
	workspace := t.TempDir()
	gate := filepath.Join(workspace, "setup-gate")
	if err := syscall.Mkfifo(gate, 0o600); err != nil {
		t.Fatal(err)
	}
	// Linux opens a FIFO read/write without waiting for a peer. Keeping this
	// handle open also lets teardown release a script that has not reached read.
	pipe, err := os.OpenFile(gate, os.O_RDWR, 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = pipe.Close() })
	proxy := modelConfigServer(t)
	r := newSessionTestRuntimeWithState(t, lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"), store)
	var once sync.Once
	release := func() {
		once.Do(func() {
			if _, err := pipe.WriteString("done\n"); err != nil {
				t.Error(err)
			}
		})
	}
	t.Cleanup(release)
	request := openLifecycleSession("session-1", filepath.Join(workspace, "project"))
	script := fmt.Sprintf("printf ready; IFS= read -r gate < %s", shellQuote(gate))
	request.SetupScript = &script
	if _, err := r.CreateSession(sessionCallContext(r, context.Background()), request); err != nil {
		t.Fatal(err)
	}
	s := r.sessions.Get("session-1")
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	var cursor uint64
	for {
		entries, err := s.EventJournal().Since(ctx, cursor, 128)
		if err != nil {
			t.Fatal(err)
		}
		for _, entry := range entries {
			cursor = entry.Cursor
			if entry.Event.GetSetupOutput() != nil {
				return r, s, release
			}
		}
		if err := s.EventJournal().WaitBeyond(ctx, cursor); err != nil {
			t.Fatal(err)
		}
	}
}

func sessionStopping(r *Runtime, s *session.Session) bool {
	current, found, err := session.ReadLifecycleStatus(r.lifecycleContext, r.lifecycleState, s.ID())
	return err == nil && found && current.Phase == session.Stopping
}

func waitLifecycle(t *testing.T, r *Runtime, predicate func() bool, want string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	for {
		changed := r.changesSince()
		statusBefore := currentSessionLifecycleStatus(r)
		statusCtx, stopStatusWatch := context.WithCancel(ctx)
		statusChanged := make(chan error, 1)
		go func() {
			statusChanged <- lifecycle.WatchStatus(statusCtx, r.lifecycleState, func(current lifecycle.Status) (bool, error) {
				return current != statusBefore, nil
			})
		}()
		if predicate() {
			stopStatusWatch()
			return
		}
		select {
		case <-changed:
		case err := <-statusChanged:
			if err != nil && !errors.Is(err, context.Canceled) {
				stopStatusWatch()
				t.Fatalf("watch lifecycle status: %v", err)
			}
		case <-ctx.Done():
			stopStatusWatch()
			t.Fatalf("timed out waiting for %s: desired_state=%+v runtime=%+v", want, environmentDesired(r), currentSessionLifecycleStatus(r))
		}
		stopStatusWatch()
	}
}

func sessionLifecycleSnapshot(t *testing.T, r *Runtime) *sessionpb.EnvironmentRuntimeLifecycleStatus {
	t.Helper()
	response, err := r.GetStatus(sessionCallContext(r, context.Background()), &sessionpb.GetStatusRequest{})
	if err != nil {
		t.Fatalf("GetStatus: %v", err)
	}
	if response.Lifecycle == nil {
		t.Fatal("GetStatus omitted lifecycle status")
	}
	return response.Lifecycle
}

func TestEnvironmentRuntimeLifecycleStatusOmitsStaleBlockerAndError(t *testing.T) {
	resourceError := "resource failure"
	current := sessionLifecycleStatus(
		lifecycle.EnvironmentDesired{Mode: lifecycle.Quiescing, Generation: 8}, true,
		lifecycle.Status{
			Generation: 8,
			Stage:      lifecycle.Halting,
			Blocker: lifecycle.Blocker{
				Kind:  lifecycle.SessionHaltPending,
				Error: resourceError,
			},
		}, true,
	)
	if current.Blocker == nil || current.Blocker.Error != resourceError {
		t.Fatalf("current blocker error was not projected: %+v", current)
	}

	status := sessionLifecycleStatus(
		lifecycle.EnvironmentDesired{Mode: lifecycle.Quiescing, Generation: 9}, true,
		lifecycle.Status{
			Generation: 8,
			Stage:      lifecycle.Flushing,
			Error:      "old lifecycle error",
			Blocker: lifecycle.Blocker{
				Kind:  lifecycle.IngressAcknowledgementPending,
				Error: "old resource error",
			},
		}, true,
	)
	if status.ProgressCurrent || status.Error != "" || status.Blocker != nil {
		t.Fatalf("stale progress exposed as current: %+v", status)
	}
	if status.ProgressGeneration != 8 || status.Stage != sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_FLUSHING {
		t.Fatalf("stale progress snapshot = %+v, want last stage and generation", status)
	}
}

// Hold the real journal transaction so an admission remains in flight.
func holdJournalBatch(t *testing.T, ctx context.Context, s *session.Session) func() {
	t.Helper()
	held, release := make(chan struct{}), make(chan struct{})
	done := make(chan error, 1)
	go func() {
		done <- s.EventJournal().WithBatch(ctx, func(context.Context) error {
			close(held)
			<-release
			return nil
		})
	}()
	var once sync.Once
	releaseBatch := func() {
		once.Do(func() {
			close(release)
			if err := <-done; err != nil {
				t.Errorf("release journal transaction: %v", err)
			}
		})
	}
	t.Cleanup(releaseBatch)
	select {
	case <-held:
	case <-ctx.Done():
		t.Fatal("journal transaction did not start")
	}
	return releaseBatch
}

// Restore only after the failed handler has settled; production must never
// recreate missing authoritative accounting as an idle (zero) count.
func removeAdmissionState(t *testing.T, r *Runtime) func() {
	t.Helper()
	ptr := lifecycle.ResourceFor(lifecycle.EnvironmentResourceID, environmentAdmissionCountSpec{}).Metadata()
	r.mu.Lock()
	err := r.lifecycleState.Destroy(r.lifecycleContext, ptr, state.WithDestroyOwner(lifecycle.EnvironmentOwner))
	r.mu.Unlock()
	if err != nil {
		t.Fatal(err)
	}
	return func() {
		r.mu.Lock()
		err := r.lifecycleState.Create(r.lifecycleContext, lifecycle.ResourceFor(lifecycle.EnvironmentResourceID, environmentAdmissionCountSpec{}), state.WithCreateOwner(lifecycle.EnvironmentOwner))
		r.mu.Unlock()
		if err != nil {
			t.Errorf("restore admission accounting for teardown: %v", err)
		}
	}
}

// The controller creates its output; COSI watches creation and retries version conflicts.
func modifyLifecycleForTest[T lifecycle.Spec[T]](ctx context.Context, r *Runtime, id, owner string, modify func(*T)) error {
	var zero T
	ptr := lifecycle.ResourceFor(id, zero).Metadata()
	if _, err := safe.StateWatchFor[*typed.Resource[T, lifecycle.Extension[T]]](ctx, r.lifecycleState, ptr,
		state.WithEventTypes(state.Created, state.Updated)); err != nil {
		return err
	}
	_, err := safe.StateUpdateWithConflicts(ctx, r.lifecycleState, ptr, func(value *typed.Resource[T, lifecycle.Extension[T]]) error {
		modify(value.TypedSpec())
		return nil
	}, state.WithUpdateOwner(owner))
	return err
}

func TestClosingIntentPreservesSealAndWaitsForSetup(t *testing.T) {
	r, s, finishSetup := lifecycleTestBlockedSetup(t)

	if _, err := r.Quiesce(context.Background(), &sessionpb.QuiesceRequest{}); err != nil {
		t.Fatalf("accept quiesce: %v", err)
	}
	waitLifecycle(t, r, func() bool {
		current := currentSessionLifecycleStatus(r)
		return current.Stage == lifecycle.Halting && current.Blocker.Kind == lifecycle.SessionHaltPending && sessionStopping(r, s)
	}, "quiesce halt to wait at setup barrier")
	blocked := sessionLifecycleSnapshot(t, r)
	if !blocked.ProgressCurrent || blocked.Stage != sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_HALTING ||
		blocked.Blocker == nil || blocked.Blocker.Kind != sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_SESSION_HALT ||
		blocked.Blocker.ResourceId != s.ID() || blocked.Blocker.ResourceType != session.LifecycleStatusType || !blocked.Blocker.ResourcePresent {
		t.Fatalf("GetStatus while setup blocks halt = %+v", blocked)
	}

	closed := make(chan error, 1)
	go func() { closed <- r.Close(context.Background()) }()
	waitLifecycle(t, r, func() bool { return environmentDesired(r).Mode == lifecycle.Closing }, "accepted Close")
	if seal := s.EventJournal().SealState(); seal != nil {
		t.Fatalf("sealed before setup: %+v", seal)
	}
	waitLifecycle(t, r, func() bool {
		current, found, err := session.ReadLifecycleStatus(r.lifecycleContext, r.lifecycleState, s.ID())
		return err == nil && found && current.EnvironmentGeneration == environmentDesired(r).Generation
	}, "Session to observe Closing generation")
	beforeCutoff, found, err := session.ReadLifecycleStatus(r.lifecycleContext, r.lifecycleState, s.ID())
	if err != nil || !found || beforeCutoff.EnvironmentGeneration != environmentDesired(r).Generation || beforeCutoff.AdmissionClosed {
		t.Fatalf("Session cutoff reported during halt: status=%+v found=%v err=%v", beforeCutoff, found, err)
	}
	finishSetup()
	waitLifecycle(t, r, func() bool { return currentSessionLifecycleStatus(r).Stage == lifecycle.Flushing }, "closing seal flush")
	seal := s.EventJournal().SealState()
	if seal == nil || seal.IncarnationUID != "test-incarnation" {
		t.Fatalf("Close lost seal: %+v", seal)
	}
	ack, err := r.store.ReadAcknowledgement(s.ID())
	if err != nil || ack != nil {
		t.Fatalf("Close unexpectedly acknowledged ingress: ack=%+v err=%v", ack, err)
	}
	select {
	case err := <-closed:
		t.Fatalf("Close finished without exact History acknowledgement: %v", err)
	default:
	}
	if err := r.store.WriteAcknowledgement(s.ID(), retained.Acknowledgement{Cursor: seal.Cursor, Sealed: true}); err != nil {
		t.Fatal(err)
	}
	if err := waitClose(t, closed); err != nil {
		t.Fatal(err)
	}
}

func TestQuiesceWaitsForExactSealedCursorAcknowledgement(t *testing.T) {
	r, s := lifecycleTestSession(t)
	if err := s.Emit(context.Background(), &eventpb.Event{Observation: &eventpb.Event_HarnessStderr{HarnessStderr: &eventpb.HarnessStderr{Text: "kept"}}}, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Quiesce(context.Background(), &sessionpb.QuiesceRequest{}); err != nil {
		t.Fatalf("accept quiesce: %v", err)
	}
	waitLifecycle(t, r, func() bool { return currentSessionLifecycleStatus(r).Stage == lifecycle.Flushing }, "quiesce to flush sealed journal")
	cutoff, found, err := session.ReadLifecycleStatus(r.lifecycleContext, r.lifecycleState, s.ID())
	if err != nil || !found || cutoff.EnvironmentGeneration != environmentDesired(r).Generation || !cutoff.AdmissionClosed {
		t.Fatalf("Session cutoff after sealing: status=%+v found=%v err=%v", cutoff, found, err)
	}
	seal := s.EventJournal().SealState()
	if seal == nil || seal.Cursor == 0 {
		t.Fatalf("journal seal = %+v, want a nonzero cursor", seal)
	}
	progress, found, err := session.ReadLifecycleStatus(r.lifecycleContext, r.lifecycleState, s.ID())
	if err != nil || !found || !progress.Sealed || progress.EnvironmentGeneration != environmentDesired(r).Generation || progress.SealUID != seal.IncarnationUID || progress.SealCursor != seal.Cursor {
		t.Fatalf("Session seal status = %+v, found=%t err=%v; journal seal = %+v", progress, found, err, seal)
	}
	assertDeliveryPending := func(ack retained.Acknowledgement, present bool) {
		t.Helper()
		waitLifecycle(t, r, func() bool {
			status, found, err := session.ReadLifecycleStatus(context.Background(), r.lifecycleState, s.ID())
			return err == nil && found && status.Sealed && status.SealUID == seal.IncarnationUID &&
				status.SealCursor == seal.Cursor && !status.Delivered &&
				status.AcknowledgementPresent == present && status.AcknowledgementSealed == ack.Sealed &&
				status.AcknowledgementCursor == ack.Cursor && currentSessionLifecycleStatus(r).Stage == lifecycle.Flushing
		}, "Session delivery to remain pending")
	}
	assertDeliveryPending(retained.Acknowledgement{}, false)
	for _, ack := range []retained.Acknowledgement{
		{Cursor: seal.Cursor, Sealed: false},
		{Cursor: seal.Cursor + 1, Sealed: true},
	} {
		if err := r.store.WriteAcknowledgement(s.ID(), ack); err != nil {
			t.Fatal(err)
		}
		assertDeliveryPending(ack, true)
	}
	if err := r.store.WriteAcknowledgement(s.ID(), retained.Acknowledgement{Cursor: seal.Cursor - 1, Sealed: true}); err != nil {
		t.Fatal(err)
	}
	assertDeliveryPending(retained.Acknowledgement{Cursor: seal.Cursor - 1, Sealed: true}, true)
	waitLifecycle(t, r, func() bool {
		current := currentSessionLifecycleStatus(r)
		return current.Stage == lifecycle.Flushing && current.Blocker.Kind == lifecycle.IngressAcknowledgementPending &&
			current.Blocker.AcknowledgementPresent && current.Blocker.AcknowledgementCursor == seal.Cursor-1
	}, "publish the under-cursor acknowledgement blocker")
	blocked := sessionLifecycleSnapshot(t, r)
	if !blocked.ProgressCurrent || blocked.Stage != sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_FLUSHING ||
		blocked.Blocker == nil || blocked.Blocker.Kind != sessionpb.EnvironmentRuntimeLifecycleBlockerKind_ENVIRONMENT_RUNTIME_LIFECYCLE_BLOCKER_KIND_INGRESS_ACKNOWLEDGEMENT ||
		blocked.Blocker.ResourceId != s.ID() || blocked.Blocker.ResourceType != retained.IngressAckType ||
		!blocked.Blocker.ResourcePresent || !blocked.Blocker.AcknowledgementPresent || !blocked.Blocker.AcknowledgementSealed ||
		blocked.Blocker.AcknowledgementCursor != seal.Cursor-1 || blocked.Blocker.JournalSealCursor != seal.Cursor ||
		!blocked.Blocker.JournalSealPresent {
		t.Fatalf("GetStatus under-cursor acknowledgement = %+v", blocked)
	}
	facts, err := r.facts()
	if err != nil {
		t.Fatal(err)
	}
	if facts.QuiesceStage != environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_FLUSHING {
		t.Fatalf("facts after under-cursor acknowledgement = %s, want FLUSHING", facts.QuiesceStage)
	}
	if current := currentSessionLifecycleStatus(r); current.Stage != lifecycle.Flushing {
		t.Fatalf("runtime after under-cursor acknowledgement = %v, want FLUSHING", current.Stage)
	}

	if err := r.store.WriteAcknowledgement(s.ID(), retained.Acknowledgement{Cursor: seal.Cursor, Sealed: true}); err != nil {
		t.Fatal(err)
	}
	waitLifecycle(t, r, func() bool { return currentSessionLifecycleStatus(r).Stage == lifecycle.Quiesced }, "exact sealed cursor acknowledgement")
	status, found, err := session.ReadLifecycleStatus(context.Background(), r.lifecycleState, s.ID())
	if err != nil || !found || !status.Delivered || status.SealUID != seal.IncarnationUID || status.SealCursor != seal.Cursor {
		t.Fatalf("Session delivery = %+v, found %t, err %v", status, found, err)
	}
	quiesced := sessionLifecycleSnapshot(t, r)
	if quiesced.Stage != sessionpb.EnvironmentRuntimeLifecycleStage_ENVIRONMENT_RUNTIME_LIFECYCLE_STAGE_QUIESCED || quiesced.Blocker != nil {
		t.Fatalf("GetStatus after exact acknowledgement = %+v, want QUIESCED without a blocker", quiesced)
	}
	facts, err = r.facts()
	if err != nil {
		t.Fatal(err)
	}
	if facts.QuiesceStage != environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_QUIESCED {
		t.Fatalf("facts after exact sealed cursor acknowledgement = %s, want QUIESCED", facts.QuiesceStage)
	}
}

func TestSessionDeliveryCompleteRequiresCurrentGenerationAndSealUID(t *testing.T) {
	desired := lifecycle.EnvironmentDesired{Generation: 7, SealUID: "current"}
	status := session.LifecycleStatus{EnvironmentGeneration: 7, AdmissionClosed: true, Sealed: true, SealUID: "current", SealCursor: 0, Delivered: true}
	if !sessionSealComplete(status, true, desired) || !sessionDeliveryComplete(status, true, desired) {
		t.Fatal("exact current-generation zero-cursor seal and delivery were rejected")
	}
	admissionOpen := status
	admissionOpen.AdmissionClosed = false
	if sessionSealComplete(admissionOpen, true, desired) {
		t.Fatal("sealed status with admission still open completed the sealing barrier")
	}
	for name, check := range map[string]func() bool{
		"missing": func() bool { return sessionDeliveryComplete(status, false, desired) },
		"stale generation": func() bool {
			stale := status
			stale.EnvironmentGeneration--
			return sessionDeliveryComplete(stale, true, desired)
		},
		"stale UID": func() bool {
			stale := status
			stale.SealUID = "previous"
			return sessionDeliveryComplete(stale, true, desired)
		},
		"admission open": func() bool {
			open := status
			open.AdmissionClosed = false
			return sessionDeliveryComplete(open, true, desired)
		},
		"unsealed": func() bool {
			unsealed := status
			unsealed.Sealed = false
			return sessionDeliveryComplete(unsealed, true, desired)
		},
		"undelivered": func() bool {
			pending := status
			pending.Delivered = false
			return sessionDeliveryComplete(pending, true, desired)
		},
	} {
		if check() {
			t.Errorf("%s status completed delivery", name)
		}
	}
}

func TestCloseWaitsForAdmission(t *testing.T) {
	r, s := lifecycleTestSession(t)
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()

	releaseBatch := holdJournalBatch(t, ctx, s)
	inputDone := make(chan error, 1)
	go func() {
		_, err := r.SubmitInput(sessionCallContext(r, ctx), &environmentruntimepb.SubmitInputRequest{SessionId: s.ID(), CommandId: "close-race", Text: "input"})
		inputDone <- err
	}()
	if err := watchSessionResource(ctx, r.lifecycleState, lifecycle.EnvironmentResourceID, func(input environmentAdmissionCountSpec) (bool, error) {
		return input.Admissions == 1, nil
	}); err != nil {
		releaseBatch()
		t.Fatalf("wait for held admission: %v", err)
	}
	facts, err := r.facts()
	if err != nil {
		t.Fatal(err)
	}
	admissionBlocked := false
	for _, blocker := range facts.AutosuspendBlockers {
		admissionBlocked = admissionBlocked || blocker.Reason == autosuspendAdmission
	}
	if !admissionBlocked || facts.IdleSince != nil {
		t.Fatalf("facts during admission = %v, want Admission blocker without idle token", facts)
	}
	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close(context.Background()) }()
	if err := lifecycle.WatchStatus(ctx, r.lifecycleState, func(runtime lifecycle.Status) (bool, error) {
		return runtime.Stage == lifecycle.Draining, nil
	}); err != nil {
		releaseBatch()
		t.Fatalf("wait for Close to drain admission: %v", err)
	}
	cursor := s.EventJournal().LastCursor()
	if _, err := r.SubmitInput(sessionCallContext(r, ctx), &environmentruntimepb.SubmitInputRequest{
		SessionId: s.ID(), CommandId: "close-after-barrier", Text: "late input",
	}); status.Code(err) != codes.Unavailable {
		releaseBatch()
		t.Fatalf("command after Close barrier = %v, want Unavailable", err)
	}
	if after := s.EventJournal().LastCursor(); after != cursor {
		releaseBatch()
		t.Fatalf("command after Close barrier changed journal cursor: %d -> %d", cursor, after)
	}
	select {
	case err := <-closeDone:
		releaseBatch()
		t.Fatalf("Close returned while admission was held: %v", err)
	default:
	}
	releaseBatch()
	if err := <-inputDone; status.Code(err) != codes.Aborted {
		t.Fatalf("late input admission error = %v, want Aborted", err)
	}
	if err := waitClose(t, closeDone); err != nil {
		t.Fatalf("Close after admission completion: %v", err)
	}
	after, found, err := lifecycle.Read[environmentAdmissionCountSpec](ctx, r.lifecycleState, lifecycle.EnvironmentResourceID)
	if err != nil || !found || after.Admissions != 0 {
		t.Fatalf("Runtime Inputs after admission completion = %+v, found=%v err=%v; want no admissions", after, found, err)
	}
}

func TestAdmissionDoesNotObserveAggregateStatusFailure(t *testing.T) {
	store := &harnessHostAdmissionProbeState{State: state.WrapCore(namespaced.NewState(inmem.Build))}
	r, s, _ := lifecycleTestBlockedSetupWithState(t, store)
	statusReadErr := errors.New("injected aggregate-status read failure")
	store.failNextStatusRead(r.lifecycleContext, statusReadErr)
	before := s.EventJournal().LastCursor()
	response, err := r.SubmitInput(sessionCallContext(r, context.Background()), &environmentruntimepb.SubmitInputRequest{
		SessionId: s.ID(), CommandId: "admission-with-status-read-failure", Text: "input",
	})
	if err != nil || response == nil || response.Admission == nil || response.Admission.Cursor <= before {
		t.Fatalf("command admission = response %v, err %v; want durable admission", response, err)
	}
	select {
	case failure := <-r.fatal:
		t.Fatalf("command admission observed aggregate status failure: %v", failure)
	default:
	}

	statusContext, cancel := context.WithCancel(context.Background())
	defer cancel()
	statusContext = sessionCallContext(r, statusContext)
	store.failNextStatusRead(statusContext, statusReadErr)
	if _, err := r.GetStatus(statusContext, &sessionpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("GetStatus after aggregate-status read failure = %v, want Unavailable", err)
	}
}

func TestClosePublicationFailureKeepsAdmissionClosed(t *testing.T) {
	store := &harnessHostAdmissionProbeState{
		State:                 state.WrapCore(namespaced.NewState(inmem.Build)),
		desiredPublicationErr: errors.New("injected desired-state publication failure"),
	}
	workspace := t.TempDir()
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false")
	config.expectCloseError = true
	r := newSessionTestRuntimeWithState(t, config, store)
	request := openLifecycleSession("session-1", filepath.Join(workspace, "project"))
	if _, err := r.CreateSession(sessionCallContext(r, context.Background()), request); err != nil {
		t.Fatal(err)
	}
	s := r.sessions.Get(request.SessionId)
	store.failDesiredPublication.Store(true)
	if err := r.Close(context.Background()); !errors.Is(err, store.desiredPublicationErr) {
		t.Fatalf("Close with failed desired-state publication = %v, want injected failure", err)
	}
	if !r.isClosing() {
		t.Fatal("failed desired-state publication cleared the local close latch")
	}

	cursor := s.EventJournal().LastCursor()
	if _, err := r.SubmitInput(sessionCallContext(r, context.Background()), &environmentruntimepb.SubmitInputRequest{
		SessionId: s.ID(), CommandId: "after-failed-close-publication", Text: "late input",
	}); status.Code(err) != codes.Unavailable {
		t.Fatalf("command after failed Close publication = %v, want Unavailable", err)
	}
	if after := s.EventJournal().LastCursor(); after != cursor {
		t.Fatalf("command after failed Close publication changed journal cursor: %d -> %d", cursor, after)
	}
	if admissions, err := r.admissionCount(context.Background()); err != nil || admissions != 0 {
		t.Fatalf("admissions after failed Close publication = %d, err %v; want zero", admissions, err)
	}
	store.failDesiredPublication.Store(false)
	if err := r.Close(context.Background()); !errors.Is(err, store.desiredPublicationErr) {
		t.Fatalf("repeated Close after failed publication = %v, want original failure", err)
	}
}

func TestMissingAdmissionStateRejectsDurableWork(t *testing.T) {
	workspace := t.TempDir()
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false")
	config.expectCloseError = true
	store := &harnessHostAdmissionProbeState{State: state.WrapCore(namespaced.NewState(inmem.Build))}
	r := newSessionTestRuntimeWithState(t, config, store)
	if _, err := r.CreateSession(sessionCallContext(r, context.Background()), openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
		t.Fatal(err)
	}
	s := r.sessions.Get("session-1")
	store.dropAdmissionUpdate.Store(true)
	defer func() {
		if err := r.lifecycleState.Create(r.lifecycleContext, lifecycle.ResourceFor(lifecycle.EnvironmentResourceID, environmentAdmissionCountSpec{}), state.WithCreateOwner(lifecycle.EnvironmentOwner)); err != nil {
			t.Errorf("restore admission accounting for teardown: %v", err)
		}
	}()
	// The deferred test-only restoration permits cleanup, but must not clear the
	// Runtime failure raised when admission accounting could not be published.
	t.Cleanup(func() {
		if err := closeRuntimeForTest(t, r); !state.IsNotFoundError(err) {
			t.Errorf("Close after missing admission accounting = %v, want original failure", err)
		}
		owner, err := openTestRuntimeStore(r.config.StateDir)
		if err != nil {
			t.Errorf("settled cleanup retained the environment owner: %v", err)
			return
		}
		_ = owner.Close()
	})
	cursor := s.EventJournal().LastCursor()
	_, admissionErr := r.SubmitInput(sessionCallContext(r, r.lifecycleContext), &environmentruntimepb.SubmitInputRequest{SessionId: s.ID(), CommandId: "missing-accounting", Text: "input"})
	if status.Code(admissionErr) != codes.Unavailable {
		t.Fatalf("command with missing accounting = %v, want Unavailable", admissionErr)
	}
	if store.dropAdmissionUpdate.Load() {
		t.Fatal("command did not reach admission accounting")
	}
	r.mu.Lock()
	failure := r.failureLocked()
	r.mu.Unlock()
	if !state.IsNotFoundError(failure) {
		t.Fatalf("accounting failure = %v, want original NotFound cause", failure)
	}
	if !strings.Contains(status.Convert(admissionErr).Message(), failure.Error()) {
		t.Fatalf("command rejection = %v, want the accounting cause %v", admissionErr, failure)
	}
	if facts, err := r.facts(); err == nil {
		t.Fatalf("missing accounting returned facts: %v", facts)
	}
	if after := s.EventJournal().LastCursor(); after != cursor {
		t.Fatalf("rejected command changed journal cursor: %d -> %d", cursor, after)
	}
	request := openLifecycleSession("new-session", filepath.Join(r.config.WorkspaceRoot, "project"))
	if _, err := r.CreateSession(sessionCallContext(r, r.lifecycleContext), request); status.Code(err) != codes.Unavailable {
		t.Fatalf("Session reservation with missing accounting = %v, want Unavailable", err)
	}
	if exists, err := r.store.Exists(request.SessionId); err != nil || exists {
		t.Fatalf("rejected Session was persisted: exists=%v err=%v", exists, err)
	}
}

func TestAdmissionCompletionStateFailureIsReported(t *testing.T) {
	workspace := t.TempDir()
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false")
	config.expectCloseError = true
	r := newSessionTestRuntime(t, config)
	if _, err := r.CreateSession(sessionCallContext(r, context.Background()), openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
		t.Fatal(err)
	}
	s := r.sessions.Get("session-1")
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	releaseBatch := holdJournalBatch(t, ctx, s)
	inputDone := make(chan error, 1)
	go func() {
		_, err := r.SubmitInput(sessionCallContext(r, ctx), &environmentruntimepb.SubmitInputRequest{SessionId: s.ID(), CommandId: "completion-accounting", Text: "input"})
		inputDone <- err
	}()
	if err := watchSessionResource(ctx, r.lifecycleState, lifecycle.EnvironmentResourceID, func(input environmentAdmissionCountSpec) (bool, error) {
		return input.Admissions == 1, nil
	}); err != nil {
		t.Fatal(err)
	}
	restore := removeAdmissionState(t, r)
	defer restore()
	releaseBatch()
	select {
	case err := <-inputDone:
		if status.Code(err) != codes.Unavailable {
			t.Fatalf("completion with missing accounting = %v, want Unavailable", err)
		}
	case <-ctx.Done():
		t.Fatal("admission did not finish")
	}
	select {
	case err := <-r.fatal:
		if !state.IsNotFoundError(err) {
			t.Fatalf("Runtime failure = %v, want missing admission state", err)
		}
	default:
		t.Fatal("accounting completion failure did not reach Runtime supervision")
	}
	if facts, err := r.facts(); err == nil {
		t.Fatalf("failed completion returned facts: %v", facts)
	}
}

func TestCloseProgressesForEmptyRunner(t *testing.T) {
	r := newBareRunner(t)
	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close(context.Background()) }()
	if err := waitClose(t, closeDone); err != nil {
		t.Fatalf("Close empty Runtime: %v", err)
	}
	current, found, err := lifecycle.ReadStatus(context.Background(), r.lifecycleState)
	if err != nil || !found || current.Stage != lifecycle.Closed {
		t.Fatalf("empty Runtime status after Close = %+v, found=%v err=%v; want CLOSED", current, found, err)
	}
}

func TestJournalAndAcknowledgementChangesWakeObservers(t *testing.T) {
	r, s := lifecycleTestSession(t)
	changes := r.changesSince()
	inventoryChanges := r.sessions.InventoryChangesSince()
	if err := s.Emit(r.lifecycleContext, &eventpb.Event{Observation: &eventpb.Event_HarnessStderr{HarnessStderr: &eventpb.HarnessStderr{Text: "journal update"}}}, nil); err != nil {
		t.Fatal(err)
	}
	ack := retained.Acknowledgement{Cursor: s.EventJournal().LastCursor()}
	if err := r.store.WriteAcknowledgement(s.ID(), ack); err != nil {
		t.Fatal(err)
	}
	r.changed() // Acknowledgement receipt still wakes observer-only consumers.
	select {
	case <-changes:
	default:
		t.Fatal("journal/acknowledgement changes did not wake Runtime observers")
	}
	select {
	case <-inventoryChanges:
		t.Fatal("journal/acknowledgement changes woke ingress inventory observers")
	default:
	}
}

func TestNewIncarnationWaitsForTeardown(t *testing.T) {
	r, s, finishSetup := lifecycleTestBlockedSetup(t)
	if _, err := r.Quiesce(context.Background(), &sessionpb.QuiesceRequest{}); err != nil {
		t.Fatal(err)
	}
	waitLifecycle(t, r, func() bool {
		return currentSessionLifecycleStatus(r).Stage == lifecycle.Halting && sessionStopping(r, s)
	}, "halt waiting for setup")
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	adoptionCtx, cancelAdoption := context.WithCancel(ctx)
	adopted := make(chan error, 1)
	go func() { adopted <- r.adoptIncarnation(adoptionCtx, "replacement-incarnation") }()
	if environmentDesired(r).Mode != lifecycle.Quiescing || sessionAdmissionOpen(r) {
		t.Fatal("replacement reopened admission before setup settled")
	}
	cancelAdoption()
	if err := waitClose(t, adopted); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled adoption: %v", err)
	}
	finishSetup()
	if err := r.waitSessionStage(ctx, lifecycle.Flushing); err != nil {
		t.Fatal(err)
	}
	seal := s.EventJournal().SealState()
	if seal == nil {
		t.Fatal("old incarnation has no seal")
	}
	if err := r.store.WriteAcknowledgement(s.ID(), retained.Acknowledgement{Cursor: seal.Cursor, Sealed: true}); err != nil {
		t.Fatal(err)
	}
	if err := r.adoptIncarnation(ctx, "replacement-incarnation"); err != nil {
		t.Fatal(err)
	}
	if seal := s.EventJournal().SealState(); seal != nil {
		t.Fatalf("new incarnation did not reopen old seal: %+v", seal)
	}
	if !sessionAdmissionOpen(r) {
		t.Fatal("new incarnation did not reopen admission")
	}
	waitLifecycle(t, r, func() bool {
		progress, found, err := session.ReadLifecycleStatus(r.lifecycleContext, r.lifecycleState, s.ID())
		return err == nil && found && progress.EnvironmentGeneration == environmentDesired(r).Generation
	}, "successor Session generation")
	progress, found, err := session.ReadLifecycleStatus(r.lifecycleContext, r.lifecycleState, s.ID())
	if err != nil || !found || progress.EnvironmentGeneration != environmentDesired(r).Generation || progress.Sealed || progress.SealUID != "" {
		t.Fatalf("successor Session status retained predecessor seal: %+v, found=%t err=%v", progress, found, err)
	}
}

func TestAcceptedSetupCanWaitForBootstrapDuringDrain(t *testing.T) {
	r, _, finishSetup := lifecycleTestBlockedSetup(t)
	called := false
	r.bootstrapWait = func(context.Context) error {
		called = true
		return nil
	}
	if _, err := r.Quiesce(context.Background(), &sessionpb.QuiesceRequest{}); err != nil {
		t.Fatal(err)
	}
	if sessionAdmissionOpen(r) {
		t.Fatal("quiesce left new work admitted")
	}
	if err := r.waitBootstrapForSession(context.Background()); err != nil {
		t.Fatalf("accepted setup lost its bootstrap dependency during drain: %v", err)
	}
	if !called {
		t.Fatal("accepted setup did not reach Runtime bootstrap")
	}
	finishSetup()
}
