package runtime

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
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
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

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

func lifecycleTestSession(t *testing.T) (*Runtime, *session.Session) {
	t.Helper()
	workspace := t.TempDir()
	proxy := modelConfigServer(t)
	r := newSessionTestRuntime(t, lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"))
	if _, err := r.OpenSession(sessionCallContext(r, context.Background()), openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
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
	if _, err := r.OpenSession(sessionCallContext(r, context.Background()), request); err != nil {
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
	ptr := lifecycleResourceFor(lifecycle.EnvironmentResourceID, environmentAdmissionCountSpec{}).Metadata()
	r.mu.Lock()
	err := r.lifecycleState.Destroy(r.lifecycleContext, ptr, state.WithDestroyOwner(lifecycle.EnvironmentOwner))
	r.mu.Unlock()
	if err != nil {
		t.Fatal(err)
	}
	return func() {
		r.mu.Lock()
		err := r.lifecycleState.Create(r.lifecycleContext, lifecycleResourceFor(lifecycle.EnvironmentResourceID, environmentAdmissionCountSpec{}), state.WithCreateOwner(lifecycle.EnvironmentOwner))
		r.mu.Unlock()
		if err != nil {
			t.Errorf("restore admission accounting for teardown: %v", err)
		}
	}
}

// The controller creates its output; COSI watches creation and retries version conflicts.
func modifyLifecycleForTest[T lifecycleSpec[T]](ctx context.Context, r *Runtime, id, owner string, modify func(*T)) error {
	var zero T
	ptr := lifecycleResourceFor(id, zero).Metadata()
	if _, err := safe.StateWatchFor[*typed.Resource[T, lifecycleExtension[T]]](ctx, r.lifecycleState, ptr,
		state.WithEventTypes(state.Created, state.Updated)); err != nil {
		return err
	}
	_, err := safe.StateUpdateWithConflicts(ctx, r.lifecycleState, ptr, func(value *typed.Resource[T, lifecycleExtension[T]]) error {
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
	seal := s.EventJournal().SealState()
	if seal == nil || seal.Cursor == 0 {
		t.Fatalf("journal seal = %+v, want a nonzero cursor", seal)
	}
	if err := r.store.WriteAcknowledgement(s.ID(), retained.Acknowledgement{Cursor: seal.Cursor - 1, Sealed: true}); err != nil {
		t.Fatal(err)
	}
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
	after, found, err := lifecycleRead[environmentAdmissionCountSpec](ctx, r.lifecycleState, lifecycle.EnvironmentResourceID)
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
	if _, err := r.OpenSession(sessionCallContext(r, context.Background()), request); err != nil {
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
	r, s := lifecycleTestSession(t)
	restore := removeAdmissionState(t, r)
	defer restore()
	if facts, err := r.facts(); err == nil {
		t.Fatalf("missing accounting returned facts: %v", facts)
	}
	cursor := s.EventJournal().LastCursor()
	if _, err := r.SubmitInput(sessionCallContext(r, r.lifecycleContext), &environmentruntimepb.SubmitInputRequest{SessionId: s.ID(), CommandId: "missing-accounting", Text: "input"}); status.Code(err) != codes.Unavailable {
		t.Fatalf("command with missing accounting = %v, want Unavailable", err)
	}
	if after := s.EventJournal().LastCursor(); after != cursor {
		t.Fatalf("rejected command changed journal cursor: %d -> %d", cursor, after)
	}
	request := openLifecycleSession("new-session", filepath.Join(r.config.WorkspaceRoot, "project"))
	if _, err := r.OpenSession(sessionCallContext(r, r.lifecycleContext), request); status.Code(err) != codes.Unavailable {
		t.Fatalf("Session reservation with missing accounting = %v, want Unavailable", err)
	}
	if exists, err := r.store.Exists(request.SessionId); err != nil || exists {
		t.Fatalf("rejected Session was persisted: exists=%v err=%v", exists, err)
	}
}

func TestAdmissionCompletionStateFailureIsReported(t *testing.T) {
	r, s := lifecycleTestSession(t)
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
