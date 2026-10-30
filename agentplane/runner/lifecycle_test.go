package runner

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
	"testing/synctest"
	"time"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerstatepb "github.com/agentydragon/ducktape/agentplane/proto/runner_statepb"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"github.com/agentydragon/ducktape/agentplane/runner/session"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/resource/typed"
	"github.com/cosi-project/runtime/pkg/safe"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const lifecycleTestTimeout = 10 * time.Second

type admissionStatusReadFailure struct {
	context context.Context
	err     error
}

type runnerAdmissionProbeState struct {
	state.State
	statusReadMu           sync.Mutex
	statusReadFailures     []admissionStatusReadFailure
	failDesiredPublication atomic.Bool
	desiredPublicationErr  error
}

func (s *runnerAdmissionProbeState) failNextStatusRead(ctx context.Context, err error) {
	s.statusReadMu.Lock()
	defer s.statusReadMu.Unlock()
	s.statusReadFailures = append(s.statusReadFailures, admissionStatusReadFailure{context: ctx, err: err})
}

func (s *runnerAdmissionProbeState) Get(ctx context.Context, pointer resource.Pointer, options ...state.GetOption) (resource.Resource, error) {
	if pointer.Type() == runnerlifecycle.AggregateStatusType {
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

func (s *runnerAdmissionProbeState) Modify(ctx context.Context, target resource.Resource, update func(resource.Resource) error, options ...state.UpdateOption) error {
	if target.Metadata().Type() == runnerlifecycle.DesiredStateType && s.failDesiredPublication.Swap(false) {
		return s.desiredPublicationErr
	}
	return s.State.Modify(ctx, target, update, options...)
}

func lifecycleTestSession(t *testing.T) (*Runner, *session.Session) {
	t.Helper()
	workspace := t.TempDir()
	proxy := modelConfigServer(t)
	r := newLifecycleRunner(t, lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"))
	if _, err := r.OpenSession(context.Background(), openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
		t.Fatal(err)
	}
	return r, r.sessions.Get("session-1")
}

func lifecycleTestBlockedSetup(t *testing.T) (*Runner, *session.Session, func()) {
	t.Helper()
	return lifecycleTestBlockedSetupWithState(t, nil)
}

func lifecycleTestBlockedSetupWithState(t *testing.T, store state.State) (*Runner, *session.Session, func()) {
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
	r := newLifecycleRunnerWithState(t, lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"), store)
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
	if _, err := r.OpenSession(context.Background(), request); err != nil {
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

func sessionStopping(r *Runner, s *session.Session) bool {
	current, found, err := session.ReadRuntime(r.ctx, r.lifecycle.state, s.ID())
	return err == nil && found && current.Phase == session.Stopping
}

func waitLifecycle(t *testing.T, r *Runner, predicate func() bool, want string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	for {
		changed := r.changesSince()
		if predicate() {
			return
		}
		select {
		case <-changed:
		case <-ctx.Done():
			t.Fatalf("timed out waiting for %s: desired_state=%+v runtime=%+v", want, r.lifecycle.desiredState(), r.lifecycle.status())
		}
	}
}

func waitClose(t *testing.T, done <-chan error) error {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	select {
	case err := <-done:
		return err
	case <-ctx.Done():
		t.Fatal("runner Close did not finish")
		return ctx.Err()
	}
}

func runnerLifecycleSnapshot(t *testing.T, r *Runner) *runnerpb.RunnerLifecycleStatus {
	t.Helper()
	response, err := r.GetStatus(context.Background(), &runnerpb.GetStatusRequest{})
	if err != nil {
		t.Fatalf("GetStatus: %v", err)
	}
	if response.Lifecycle == nil {
		t.Fatal("GetStatus omitted lifecycle status")
	}
	return response.Lifecycle
}

func TestRunnerLifecycleStatusOmitsStaleBlockerAndError(t *testing.T) {
	resourceError := "resource failure"
	current := runnerLifecycleStatus(
		runnerlifecycle.Desired{Mode: runnerlifecycle.Quiescing, Generation: 8}, true,
		runnerlifecycle.Status{
			Generation: 8,
			Stage:      runnerlifecycle.Halting,
			Blocker: runnerlifecycle.Blocker{
				Kind:  runnerlifecycle.SessionHaltPending,
				Error: resourceError,
			},
		}, true,
	)
	if current.Blocker == nil || current.Blocker.Error != resourceError {
		t.Fatalf("current blocker error was not projected: %+v", current)
	}

	status := runnerLifecycleStatus(
		runnerlifecycle.Desired{Mode: runnerlifecycle.Quiescing, Generation: 9}, true,
		runnerlifecycle.Status{
			Generation: 8,
			Stage:      runnerlifecycle.Flushing,
			Error:      "old lifecycle error",
			Blocker: runnerlifecycle.Blocker{
				Kind:  runnerlifecycle.IngressAcknowledgementPending,
				Error: "old resource error",
			},
		}, true,
	)
	if status.ProgressCurrent || status.Error != "" || status.Blocker != nil {
		t.Fatalf("stale progress exposed as current: %+v", status)
	}
	if status.ProgressGeneration != 8 || status.Stage != runnerpb.RunnerLifecycleStage_RUNNER_LIFECYCLE_STAGE_FLUSHING {
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
func removeAdmissionState(t *testing.T, r *Runner) func() {
	t.Helper()
	ptr := lifecycleResourceFor("runner", runnerInputSpec{}).Metadata()
	r.mu.Lock()
	err := r.lifecycle.state.Destroy(r.ctx, ptr, state.WithDestroyOwner("runner-host"))
	r.mu.Unlock()
	if err != nil {
		t.Fatal(err)
	}
	return func() {
		r.mu.Lock()
		err := r.lifecycle.state.Create(r.ctx, lifecycleResourceFor("runner", runnerInputSpec{}), state.WithCreateOwner("runner-host"))
		r.mu.Unlock()
		if err != nil {
			t.Errorf("restore admission accounting for teardown: %v", err)
		}
	}
}

// The controller creates its output; COSI watches creation and retries version conflicts.
func modifyLifecycleForTest[T lifecycleSpec[T]](ctx context.Context, r *Runner, id, owner string, modify func(*T)) error {
	var zero T
	ptr := lifecycleResourceFor(id, zero).Metadata()
	if _, err := safe.StateWatchFor[*typed.Resource[T, lifecycleExtension[T]]](ctx, r.lifecycle.state, ptr,
		state.WithEventTypes(state.Created, state.Updated)); err != nil {
		return err
	}
	_, err := safe.StateUpdateWithConflicts(ctx, r.lifecycle.state, ptr, func(value *typed.Resource[T, lifecycleExtension[T]]) error {
		modify(value.TypedSpec())
		return nil
	}, state.WithUpdateOwner(owner))
	return err
}

func TestCloseAfterQuiescePreservesSealAndWaitsForSetup(t *testing.T) {
	r, s, finishSetup := lifecycleTestBlockedSetup(t)

	if _, err := r.Quiesce(context.Background(), &runnerpb.QuiesceRequest{}); err != nil {
		t.Fatalf("accept quiesce: %v", err)
	}
	waitLifecycle(t, r, func() bool {
		current := r.lifecycle.status()
		return current.Stage == runnerlifecycle.Halting && current.Blocker.Kind == runnerlifecycle.SessionHaltPending && sessionStopping(r, s)
	}, "quiesce halt to wait at setup barrier")
	blocked := runnerLifecycleSnapshot(t, r)
	if !blocked.ProgressCurrent || blocked.Stage != runnerpb.RunnerLifecycleStage_RUNNER_LIFECYCLE_STAGE_HALTING ||
		blocked.Blocker == nil || blocked.Blocker.Kind != runnerpb.RunnerLifecycleBlockerKind_RUNNER_LIFECYCLE_BLOCKER_KIND_SESSION_HALT ||
		blocked.Blocker.ResourceId != s.ID() || blocked.Blocker.ResourceType != session.RuntimeType || !blocked.Blocker.ResourcePresent {
		t.Fatalf("GetStatus while setup blocks halt = %+v", blocked)
	}

	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close(context.Background()) }()
	waitLifecycle(t, r, func() bool {
		desiredState := r.lifecycle.desiredState()
		return desiredState.Mode == runnerlifecycle.Closing && desiredState.SealUID == "test-incarnation" &&
			r.lifecycle.status().Stage == runnerlifecycle.Halting && sessionStopping(r, s)
	}, "Close to preserve the accepted seal while setup is running")
	select {
	case err := <-closeDone:
		t.Fatalf("Close returned before setup completed: %v", err)
	default:
	}
	if seal := s.EventJournal().SealState(); seal != nil {
		t.Fatalf("journal sealed before setup completed: %+v", seal)
	}

	finishSetup()
	if err := waitClose(t, closeDone); err != nil {
		t.Fatalf("Close after setup completion: %v", err)
	}
	closed := runnerLifecycleSnapshot(t, r)
	if closed.Stage != runnerpb.RunnerLifecycleStage_RUNNER_LIFECYCLE_STAGE_CLOSED || closed.Blocker != nil {
		t.Fatalf("GetStatus after setup resolution = %+v, want CLOSED without a blocker", closed)
	}
	seal := s.EventJournal().SealState()
	if seal == nil || seal.IncarnationUID != "test-incarnation" {
		t.Fatalf("journal seal = %+v, want accepted incarnation", seal)
	}
	ack, err := r.store.ReadAcknowledgement(s.ID())
	if err != nil || ack != nil {
		t.Fatalf("Close unexpectedly acknowledged ingress: ack=%+v err=%v", ack, err)
	}
}

func TestQuiesceWaitsForExactSealedCursorAcknowledgement(t *testing.T) {
	r, s := lifecycleTestSession(t)
	if err := s.Emit(context.Background(), &eventpb.Event{Observation: &eventpb.Event_HarnessStderr{HarnessStderr: &eventpb.HarnessStderr{Text: "kept"}}}, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Quiesce(context.Background(), &runnerpb.QuiesceRequest{}); err != nil {
		t.Fatalf("accept quiesce: %v", err)
	}
	waitLifecycle(t, r, func() bool { return r.lifecycle.status().Stage == runnerlifecycle.Flushing }, "quiesce to flush sealed journal")
	seal := s.EventJournal().SealState()
	if seal == nil || seal.Cursor == 0 {
		t.Fatalf("journal seal = %+v, want a nonzero cursor", seal)
	}
	if err := r.lifecycle.publishAck(s.ID(), retained.Acknowledgement{Cursor: seal.Cursor - 1, Sealed: true}); err != nil {
		t.Fatal(err)
	}
	waitLifecycle(t, r, func() bool {
		current := r.lifecycle.status()
		return current.Stage == runnerlifecycle.Flushing && current.Blocker.Kind == runnerlifecycle.IngressAcknowledgementPending &&
			current.Blocker.AcknowledgementPresent && current.Blocker.AcknowledgementCursor == seal.Cursor-1
	}, "publish the under-cursor acknowledgement blocker")
	blocked := runnerLifecycleSnapshot(t, r)
	if !blocked.ProgressCurrent || blocked.Stage != runnerpb.RunnerLifecycleStage_RUNNER_LIFECYCLE_STAGE_FLUSHING ||
		blocked.Blocker == nil || blocked.Blocker.Kind != runnerpb.RunnerLifecycleBlockerKind_RUNNER_LIFECYCLE_BLOCKER_KIND_INGRESS_ACKNOWLEDGEMENT ||
		blocked.Blocker.ResourceId != s.ID() || blocked.Blocker.ResourceType != ingressAckType ||
		!blocked.Blocker.ResourcePresent || !blocked.Blocker.AcknowledgementPresent || !blocked.Blocker.AcknowledgementSealed ||
		blocked.Blocker.AcknowledgementCursor != seal.Cursor-1 || blocked.Blocker.JournalSealCursor != seal.Cursor ||
		!blocked.Blocker.JournalSealPresent {
		t.Fatalf("GetStatus under-cursor acknowledgement = %+v", blocked)
	}
	facts, err := r.facts()
	if err != nil {
		t.Fatal(err)
	}
	if facts.QuiesceStage != runnerstatepb.RunnerFacts_QUIESCE_STAGE_FLUSHING {
		t.Fatalf("facts after under-cursor acknowledgement = %s, want FLUSHING", facts.QuiesceStage)
	}
	if current := r.lifecycle.status(); current.Stage != runnerlifecycle.Flushing {
		t.Fatalf("runtime after under-cursor acknowledgement = %v, want FLUSHING", current.Stage)
	}

	if err := r.lifecycle.publishAck(s.ID(), retained.Acknowledgement{Cursor: seal.Cursor, Sealed: true}); err != nil {
		t.Fatal(err)
	}
	waitLifecycle(t, r, func() bool { return r.lifecycle.status().Stage == runnerlifecycle.Quiesced }, "exact sealed cursor acknowledgement")
	quiesced := runnerLifecycleSnapshot(t, r)
	if quiesced.Stage != runnerpb.RunnerLifecycleStage_RUNNER_LIFECYCLE_STAGE_QUIESCED || quiesced.Blocker != nil {
		t.Fatalf("GetStatus after exact acknowledgement = %+v, want QUIESCED without a blocker", quiesced)
	}
	facts, err = r.facts()
	if err != nil {
		t.Fatal(err)
	}
	if facts.QuiesceStage != runnerstatepb.RunnerFacts_QUIESCE_STAGE_QUIESCED {
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
		_, err := r.SubmitInput(ctx, &runnerservicepb.SubmitInputRequest{SessionId: s.ID(), CommandId: "close-race", Text: "input"})
		inputDone <- err
	}()
	if err := watchLifecycle(ctx, r.lifecycle, "runner", func(input runnerInputSpec) (bool, error) {
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
	if err := runnerlifecycle.WatchStatus(ctx, r.lifecycle.state, func(runtime runnerlifecycle.Status) (bool, error) {
		return runtime.Stage == runnerlifecycle.Draining, nil
	}); err != nil {
		releaseBatch()
		t.Fatalf("wait for Close to drain admission: %v", err)
	}
	cursor := s.EventJournal().LastCursor()
	if _, err := r.SubmitInput(ctx, &runnerservicepb.SubmitInputRequest{
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
	after, found, err := lifecycleRead[runnerInputSpec](ctx, r.lifecycle.state, "runner")
	if err != nil || !found || after.Admissions != 0 {
		t.Fatalf("RunnerInputs after admission completion = %+v, found=%v err=%v; want no admissions", after, found, err)
	}
}

func TestAdmissionDoesNotObserveAggregateStatusFailure(t *testing.T) {
	store := &runnerAdmissionProbeState{State: state.WrapCore(namespaced.NewState(inmem.Build))}
	r, s, _ := lifecycleTestBlockedSetupWithState(t, store)
	statusReadErr := errors.New("injected aggregate-status read failure")
	store.failNextStatusRead(r.ctx, statusReadErr)
	before := s.EventJournal().LastCursor()
	response, err := r.SubmitInput(context.Background(), &runnerservicepb.SubmitInputRequest{
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
	store.failNextStatusRead(statusContext, statusReadErr)
	if _, err := r.GetStatus(statusContext, &runnerpb.GetStatusRequest{}); status.Code(err) != codes.Unavailable {
		t.Fatalf("GetStatus after aggregate-status read failure = %v, want Unavailable", err)
	}
}

func TestClosePublicationFailureKeepsAdmissionClosed(t *testing.T) {
	store := &runnerAdmissionProbeState{
		State:                 state.WrapCore(namespaced.NewState(inmem.Build)),
		desiredPublicationErr: errors.New("injected desired-state publication failure"),
	}
	workspace := t.TempDir()
	proxy := modelConfigServer(t)
	r, err := newRunner(lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false"), nil, store)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		store.failDesiredPublication.Store(false)
		if r.closeDone == nil {
			if err := r.Close(context.Background()); err != nil {
				t.Errorf("close test Runner: %v", err)
			}
			return
		}
		<-r.closeDone
		if r.closeErr != nil {
			if err := r.finishClose(); err != nil {
				t.Errorf("finish test Runner after injected failure: %v", err)
			}
		}
	})
	if err := r.store.AdoptIncarnation("test-incarnation"); err != nil {
		t.Fatal(err)
	}
	if err := r.Startup(context.Background()); err != nil {
		t.Fatal(err)
	}
	request := openLifecycleSession("session-1", filepath.Join(workspace, "project"))
	if _, err := r.OpenSession(context.Background(), request); err != nil {
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
	if _, err := r.SubmitInput(context.Background(), &runnerservicepb.SubmitInputRequest{
		SessionId: s.ID(), CommandId: "after-failed-close-publication", Text: "late input",
	}); status.Code(err) != codes.Unavailable {
		t.Fatalf("command after failed Close publication = %v, want Unavailable", err)
	}
	if after := s.EventJournal().LastCursor(); after != cursor {
		t.Fatalf("command after failed Close publication changed journal cursor: %d -> %d", cursor, after)
	}
	if admissions, err := r.lifecycle.admissionCount(context.Background()); err != nil || admissions != 0 {
		t.Fatalf("admissions after failed Close publication = %d, err %v; want zero", admissions, err)
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
	if _, err := r.SubmitInput(r.ctx, &runnerservicepb.SubmitInputRequest{SessionId: s.ID(), CommandId: "missing-accounting", Text: "input"}); status.Code(err) != codes.Unavailable {
		t.Fatalf("command with missing accounting = %v, want Unavailable", err)
	}
	if after := s.EventJournal().LastCursor(); after != cursor {
		t.Fatalf("rejected command changed journal cursor: %d -> %d", cursor, after)
	}
	request := openLifecycleSession("new-session", filepath.Join(r.config.WorkspaceRoot, "project"))
	if _, err := r.OpenSession(r.ctx, request); status.Code(err) != codes.Unavailable {
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
		_, err := r.SubmitInput(ctx, &runnerservicepb.SubmitInputRequest{SessionId: s.ID(), CommandId: "completion-accounting", Text: "input"})
		inputDone <- err
	}()
	if err := watchLifecycle(ctx, r.lifecycle, "runner", func(input runnerInputSpec) (bool, error) {
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
			t.Fatalf("Runner failure = %v, want missing admission state", err)
		}
	default:
		t.Fatal("accounting completion failure did not reach Runner supervision")
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
		t.Fatalf("Close empty Runner: %v", err)
	}
	current, found, err := runnerlifecycle.ReadStatus(context.Background(), r.lifecycle.state)
	if err != nil || !found || current.Stage != runnerlifecycle.Closed {
		t.Fatalf("empty Runner runtime after Close = %+v, found=%v err=%v; want CLOSED", current, found, err)
	}
}

func TestJournalAndAcknowledgementChangesWakeObservers(t *testing.T) {
	r, s := lifecycleTestSession(t)
	changes := r.changesSince()
	if err := s.Emit(r.ctx, &eventpb.Event{Observation: &eventpb.Event_HarnessStderr{HarnessStderr: &eventpb.HarnessStderr{Text: "journal update"}}}, nil); err != nil {
		t.Fatal(err)
	}
	ack := retained.Acknowledgement{Cursor: s.EventJournal().LastCursor()}
	if err := r.store.WriteAcknowledgement(s.ID(), ack); err != nil {
		t.Fatal(err)
	}
	if err := r.lifecycle.publishAck(s.ID(), ack); err != nil {
		t.Fatal(err)
	}
	r.changed() // Acknowledgement receipt still wakes observer-only consumers.
	select {
	case <-changes:
	default:
		t.Fatal("journal/acknowledgement changes did not wake Runner observers")
	}
}

func TestNewIncarnationWaitsForTeardown(t *testing.T) {
	r, s, finishSetup := lifecycleTestBlockedSetup(t)
	if _, err := r.Quiesce(context.Background(), &runnerpb.QuiesceRequest{}); err != nil {
		t.Fatal(err)
	}
	waitLifecycle(t, r, func() bool {
		return r.lifecycle.status().Stage == runnerlifecycle.Halting && sessionStopping(r, s)
	}, "halt waiting for setup")
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	adopted := make(chan error, 1)
	go func() { adopted <- r.adoptIncarnation(ctx, "replacement-incarnation") }()
	select {
	case err := <-adopted:
		t.Fatalf("adoption overtook accepted setup: %v", err)
	default:
	}
	if r.lifecycle.desiredState().Mode != runnerlifecycle.Quiescing {
		t.Fatal("new incarnation reopened execution during teardown")
	}
	finishSetup()
	select {
	case err := <-adopted:
		if err != nil {
			t.Fatal(err)
		}
	case <-ctx.Done():
		t.Fatal("new incarnation waited for ingress acknowledgement")
	}
	if seal := s.EventJournal().SealState(); seal != nil {
		t.Fatalf("new incarnation did not reopen old seal: %+v", seal)
	}
	if !r.lifecycle.accepting() {
		t.Fatal("new incarnation did not reopen admission")
	}
}

func TestLifecycleWatchStopsWithRunner(t *testing.T) {
	runnerCtx, cancelRunner := context.WithCancel(context.Background())
	defer cancelRunner()
	l, err := newLifecycle(&Runner{ctx: runnerCtx}, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	cancelRunner()
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	err = watchLifecycle(ctx, l, "never-created", func(runnerInputSpec) (bool, error) { return false, nil })
	if !errors.Is(err, context.Canceled) {
		t.Fatalf("watch after runner cancellation = %v, want context.Canceled", err)
	}
}

func TestLifecycleWatchWaitsForResourceCreation(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		l, err := newLifecycle(&Runner{ctx: ctx}, nil, nil)
		if err != nil {
			t.Fatal(err)
		}
		done := make(chan error, 1)
		go func() {
			done <- watchLifecycle(ctx, l, "created-later", func(current runnerInputSpec) (bool, error) {
				return current.Admissions == 1, nil
			})
		}()
		synctest.Wait()
		select {
		case err := <-done:
			t.Fatalf("watch returned before resource creation: %v", err)
		default:
		}
		if err := l.state.Create(ctx, lifecycleResourceFor("created-later", runnerInputSpec{Admissions: 1}), state.WithCreateOwner("runner-host")); err != nil {
			t.Fatal(err)
		}
		synctest.Wait()
		select {
		case err := <-done:
			if err != nil {
				t.Fatalf("watch after resource creation: %v", err)
			}
		default:
			t.Fatal("watch did not finish after the resource became ready")
		}
	})
}
