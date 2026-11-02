package harnesshost

import (
	"context"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate"
	lifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/session"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func boundaryHost(t *testing.T) (*Host, context.Context) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	root := t.TempDir()
	r, err := newTestHost(Config{StateDir: root, Launch: configuration.SandboxLaunch{Sandbox: configuration.SandboxIdentity{UID: "sandbox"}}}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := r.CloseJournals(ctx); err != nil {
			t.Error(err)
		}
	})
	return r, ctx
}

func intentFor(r *Host, generation uint64, mode runtimepb.LifecycleIntent_Mode) *runtimepb.LifecycleIntent {
	intent := &runtimepb.LifecycleIntent{Binding: &runtimepb.Binding{RuntimeBootId: "runtime", Generation: generation}, SandboxUid: "sandbox", IncarnationUid: "incarnation", Mode: mode}
	if mode == runtimepb.LifecycleIntent_QUIESCING || mode == runtimepb.LifecycleIntent_CLOSING {
		intent.SealUid = intent.IncarnationUid
	}
	return intent
}

func bindingContext(ctx context.Context, binding *runtimepb.Binding) context.Context {
	return withBinding(ctx, binding)
}

func publishHostIntentStatus(ctx context.Context, r *Host, intent *runtimepb.LifecycleIntent, applied bool) error {
	value := lifecycle.HostIntentStatus{Intent: lifecycle.HostIntentFromProto(intent), Applied: applied}
	return cosi.Publish(ctx, r.lifecycle.state, lifecycle.HostIntentStatusResource(value), value, "harness-host-intent")
}

type pausedSessionInputState struct {
	state.State
	armed            atomic.Bool
	entered          chan struct{}
	release          chan struct{}
	closeReadArmed   atomic.Bool
	closeReadEntered chan struct{}
	closeReadRelease chan struct{}
}

func (s *pausedSessionInputState) Modify(ctx context.Context, value resource.Resource, update func(resource.Resource) error, opts ...state.UpdateOption) error {
	if err := s.State.Modify(ctx, value, update, opts...); err != nil {
		return err
	}
	if value.Metadata().Type() == session.ReconcileSnapshotType && s.armed.CompareAndSwap(true, false) {
		close(s.entered)
		select {
		case <-s.release:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return nil
}

func (s *pausedSessionInputState) Get(ctx context.Context, pointer resource.Pointer, opts ...state.GetOption) (resource.Resource, error) {
	value, err := s.State.Get(ctx, pointer, opts...)
	if err != nil {
		return nil, err
	}
	if pointer.Type() == lifecycle.AggregateStatusType && ctx == context.Background() && s.closeReadArmed.CompareAndSwap(true, false) {
		close(s.closeReadEntered)
		select {
		case <-s.closeReadRelease:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
	return value, nil
}

func TestHostIntentCannotReopenDesiredStateAfterClose(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	store := &pausedSessionInputState{
		State: state.WrapCore(namespaced.NewState(inmem.Build)), entered: make(chan struct{}), release: make(chan struct{}),
		closeReadEntered: make(chan struct{}), closeReadRelease: make(chan struct{}),
	}
	r, err := newTestHost(Config{StateDir: t.TempDir(), Launch: configuration.SandboxLaunch{Sandbox: configuration.SandboxIdentity{UID: "sandbox"}}}, store)
	if err != nil {
		t.Fatal(err)
	}
	var release sync.Once
	var releaseCloseRead sync.Once
	t.Cleanup(func() {
		release.Do(func() { close(store.release) })
		releaseCloseRead.Do(func() { close(store.closeReadRelease) })
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cleanupCancel()
		if err := r.CloseJournals(cleanupCtx); err != nil {
			t.Error(err)
		}
	})
	if err := r.ApplyIntent(ctx, intentFor(r, 1, runtimepb.LifecycleIntent_RUNNING)); err != nil {
		t.Fatal(err)
	}
	record, err := retained.NewRecord(&sessionpb.SessionSpec{Harness: sessionpb.Harness_HARNESS_CLAUDE, Cwd: r.config.StateDir, Model: "test"}, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.store.Write("session", record); err != nil {
		t.Fatal(err)
	}
	if _, err := r.sessions.Load(ctx, "session"); err != nil {
		t.Fatal(err)
	}
	store.armed.Store(true)
	applied := make(chan error, 1)
	go func() { applied <- r.ApplyIntent(ctx, intentFor(r, 2, runtimepb.LifecycleIntent_RUNNING)) }()
	select {
	case <-store.entered:
	case <-ctx.Done():
		t.Fatal("Host intent did not reach Session demand reset")
	}
	store.closeReadArmed.Store(true)
	closed := make(chan error, 1)
	go func() { closed <- r.CloseJournals(ctx) }()
	select {
	case <-store.closeReadEntered:
	case <-ctx.Done():
		t.Fatal("Host Close did not publish Closing")
	}
	release.Do(func() { close(store.release) })
	select {
	case err := <-applied:
		if err == nil {
			t.Fatal("an intent admitted before Close republished Running after Closing")
		}
	case <-ctx.Done():
		t.Fatal("Host intent did not settle")
	}
	if got := r.lifecycle.desiredState().Mode; got != lifecycle.Closing {
		t.Fatalf("desired state after Close = %v, want Closing", got)
	}
	releaseCloseRead.Do(func() { close(store.closeReadRelease) })
	select {
	case err := <-closed:
		if err != nil {
			t.Fatalf("Host Close after interrupted intent = %v", err)
		}
	case <-ctx.Done():
		t.Fatal("Host Close did not finish")
	}
}

func TestHostLifecycleEffectIsFencedWhileIntentIsApplying(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	store := &pausedSessionInputState{
		State: state.WrapCore(namespaced.NewState(inmem.Build)), entered: make(chan struct{}), release: make(chan struct{}),
	}
	r, err := newTestHost(Config{StateDir: t.TempDir(), Launch: configuration.SandboxLaunch{Sandbox: configuration.SandboxIdentity{UID: "sandbox"}}}, store)
	if err != nil {
		t.Fatal(err)
	}
	r.SetRuntimeBinding("runtime", true)
	var release sync.Once
	t.Cleanup(func() {
		release.Do(func() { close(store.release) })
		cleanupCtx, cleanupCancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cleanupCancel()
		if err := r.CloseJournals(cleanupCtx); err != nil {
			t.Error(err)
		}
	})
	if err := r.ApplyIntent(ctx, intentFor(r, 1, runtimepb.LifecycleIntent_RUNNING)); err != nil {
		t.Fatal(err)
	}
	record, err := retained.NewRecord(&sessionpb.SessionSpec{Harness: sessionpb.Harness_HARNESS_CLAUDE, Cwd: r.config.StateDir, Model: "test"}, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.store.Write("session", record); err != nil {
		t.Fatal(err)
	}
	if _, err := r.sessions.Load(ctx, "session"); err != nil {
		t.Fatal(err)
	}
	previous, found, err := lifecycle.ReadDesired(ctx, r.lifecycle.state)
	if err != nil || !found || previous.Mode != lifecycle.Running {
		t.Fatalf("previous running desired = %+v, found=%t, err=%v", previous, found, err)
	}
	store.armed.Store(true)
	applied := make(chan error, 1)
	go func() { applied <- r.ApplyIntent(ctx, intentFor(r, 2, runtimepb.LifecycleIntent_RUNNING)) }()
	select {
	case <-store.entered:
	case <-ctx.Done():
		t.Fatal("Host intent did not reach Session demand reset")
	}
	if r.ready.Load() {
		t.Fatal("Host admission readiness remained open during intent application")
	}
	var effectRan bool
	if err := r.admitLifecycleEffect(ctx, previous, func(context.Context) error {
		effectRan = true
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if effectRan {
		t.Fatal("old-generation lifecycle effect was admitted while a new intent was applying")
	}
	release.Do(func() { close(store.release) })
	select {
	case err := <-applied:
		if err != nil {
			t.Fatalf("apply next Host intent: %v", err)
		}
	case <-ctx.Done():
		t.Fatal("Host intent did not finish after release")
	}
	next, found, err := lifecycle.ReadDesired(ctx, r.lifecycle.state)
	if err != nil || !found || next.Generation != 2 {
		t.Fatalf("next desired = %+v, found=%t, err=%v", next, found, err)
	}
	var nextEffectRan bool
	if err := r.admitLifecycleEffect(ctx, next, func(context.Context) error {
		nextEffectRan = true
		return nil
	}); err != nil || !nextEffectRan {
		t.Fatalf("next-generation effect was not admitted: ran=%t, err=%v", nextEffectRan, err)
	}
}

func TestHostRejectsStaleGenerationBeforeDurableAdmission(t *testing.T) {
	r, ctx := boundaryHost(t)
	first := intentFor(r, 1, runtimepb.LifecycleIntent_RUNNING)
	if err := r.ApplyIntent(ctx, first); err != nil {
		t.Fatal(err)
	}
	next := intentFor(r, 2, runtimepb.LifecycleIntent_QUIESCING)
	if err := r.ApplyIntent(ctx, next); err != nil {
		t.Fatal(err)
	}
	if _, err := r.BoundContext(ctx, first.Binding); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("stale direct-call ingress returned %v", err)
	}
	_, err := r.SubmitInput(bindingContext(ctx, first.Binding), &environmentruntimepb.SubmitInputRequest{SessionId: "never-created", CommandId: "stale", Text: "must not run"})
	if status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("stale admission returned %v", err)
	}
	if ids, err := r.store.SessionIDs(); err != nil || len(ids) != 0 {
		t.Fatalf("stale request created state: %v, %v", ids, err)
	}
	changed := proto.Clone(next).(*runtimepb.LifecycleIntent)
	changed.Mode = runtimepb.LifecycleIntent_RUNNING
	changed.SealUid = ""
	if err := r.ApplyIntent(ctx, changed); err == nil {
		t.Fatal("mode changed without generation")
	}
}

func TestHostSealsOnlyAfterMatchingExecutionFence(t *testing.T) {
	r, ctx := boundaryHost(t)
	first := intentFor(r, 1, runtimepb.LifecycleIntent_RUNNING)
	if err := r.ApplyIntent(ctx, first); err != nil {
		t.Fatal(err)
	}
	record, err := retained.NewRecord(&sessionpb.SessionSpec{Harness: sessionpb.Harness_HARNESS_CLAUDE, Cwd: r.config.StateDir, Model: "test"}, "", nil)
	if err != nil {
		t.Fatal(err)
	}
	if err := r.store.Write("session", record); err != nil {
		t.Fatal(err)
	}
	s, err := r.sessions.Load(ctx, "session")
	if err != nil {
		t.Fatal(err)
	}
	// The Environment Runtime owns its persistent incarnation seal; Host drain
	// handling must not change it.
	if err := cosistate.WriteEnvironmentSeal(ctx, r.lifecycle.state, cosistate.EnvironmentSeal{IncarnationUID: "runtime-owned"}); err != nil {
		t.Fatal(err)
	}
	drain := intentFor(r, 2, runtimepb.LifecycleIntent_QUIESCING)
	if err := r.ApplyIntent(ctx, drain); err != nil {
		t.Fatal(err)
	}
	if err := r.lifecycle.waitFor(ctx, func() (bool, error) { r.mu.Lock(); defer r.mu.Unlock(); return r.nativeStopped, nil }); err != nil {
		t.Fatal(err)
	}
	if s.EventJournal().SealState() != nil {
		t.Fatal("journal sealed before Execution fence")
	}
	stale := proto.Clone(first).(*runtimepb.LifecycleIntent)
	stale.ExecutionFenced = true
	if err := r.ApplyIntent(ctx, stale); err == nil {
		t.Fatal("stale Execution fence accepted")
	}
	drain.ExecutionFenced = true
	if err := r.ApplyIntent(ctx, drain); err != nil {
		t.Fatal(err)
	}
	if err := publishHostIntentStatus(ctx, r, drain, true); err != nil {
		t.Fatal(err)
	}
	if err := r.ApplyIntent(ctx, intentFor(r, 2, runtimepb.LifecycleIntent_QUIESCING)); err == nil {
		t.Fatal("same-generation Execution fence regressed")
	}
	if err := r.lifecycle.wait(ctx, lifecycle.Flushing); err != nil {
		t.Fatal(err)
	}
	seal := s.EventJournal().SealState()
	if seal == nil || seal.IncarnationUID != "incarnation" {
		t.Fatalf("seal=%+v", seal)
	}
	ack := retained.Acknowledgement{Cursor: seal.Cursor, Sealed: true}
	if err := r.store.WriteAcknowledgement("session", ack); err != nil {
		t.Fatal(err)
	}
	if err := r.lifecycle.wait(ctx, lifecycle.Quiesced); err != nil {
		t.Fatal(err)
	}
	if seal, found, err := cosistate.ReadEnvironmentSeal(ctx, r.lifecycle.state); err != nil || !found || seal.IncarnationUID != "runtime-owned" {
		t.Fatalf("Host mutated environment seal: %+v, found=%t, err=%v", seal, found, err)
	}
}

func TestHostFenceRequiresAppliedExactIntent(t *testing.T) {
	r, _ := boundaryHost(t)
	intent := intentFor(r, 2, runtimepb.LifecycleIntent_QUIESCING)
	intent.ExecutionFenced = true
	applied := lifecycle.HostIntentStatus{Intent: lifecycle.HostIntentFromProto(intent), Applied: true}
	if !harnessHostFenceMatches(applied, intent, 2) {
		t.Fatal("matching applied Host intent was rejected")
	}
	for name, mutate := range map[string]func(*lifecycle.HostIntentStatus){
		"stale generation":  func(value *lifecycle.HostIntentStatus) { value.Intent.Generation-- },
		"runtime boot":      func(value *lifecycle.HostIntentStatus) { value.Intent.RuntimeBootID = "old-runtime" },
		"unfenced":          func(value *lifecycle.HostIntentStatus) { value.Intent.ExecutionFenced = false },
		"applying":          func(value *lifecycle.HostIntentStatus) { value.Applied = false },
		"failed":            func(value *lifecycle.HostIntentStatus) { value.Error = "failed" },
		"wrong incarnation": func(value *lifecycle.HostIntentStatus) { value.Intent.IncarnationUID = "old" },
	} {
		t.Run(name, func(t *testing.T) {
			stale := applied
			mutate(&stale)
			if harnessHostFenceMatches(stale, intent, 2) {
				t.Fatal("nonmatching applied Host intent accepted")
			}
		})
	}
	if harnessHostFenceMatches(applied, intent, 3) {
		t.Fatal("fact from an earlier desired generation accepted")
	}
}

func TestHostUsesCosiExecutionFenceFact(t *testing.T) {
	r, ctx := boundaryHost(t)
	if err := r.ApplyIntent(ctx, intentFor(r, 1, runtimepb.LifecycleIntent_RUNNING)); err != nil {
		t.Fatal(err)
	}
	drain := intentFor(r, 2, runtimepb.LifecycleIntent_QUIESCING)
	if err := r.ApplyIntent(ctx, drain); err != nil {
		t.Fatal(err)
	}
	if err := r.lifecycle.wait(ctx, lifecycle.Fencing); err != nil {
		t.Fatal(err)
	}
	drain.ExecutionFenced = true
	if err := r.ApplyIntent(ctx, drain); err != nil {
		t.Fatal(err)
	}
	if err := publishHostIntentStatus(ctx, r, drain, false); err != nil {
		t.Fatal(err)
	}
	if err := publishHostIntentStatus(ctx, r, drain, true); err != nil {
		t.Fatal(err)
	}
	if err := r.lifecycle.wait(ctx, lifecycle.Flushing); err != nil {
		t.Fatal(err)
	}
}

type bootstrapBindingRecorder struct {
	binding *runtimepb.Binding
}

func (c *bootstrapBindingRecorder) HostFailed(error) {}

func (c *bootstrapBindingRecorder) HostExecutionReady(context.Context) (bool, error) {
	return true, nil
}

func (c *bootstrapBindingRecorder) WaitHostBootstrap(_ context.Context, binding *runtimepb.Binding) error {
	c.binding = proto.Clone(binding).(*runtimepb.Binding)
	return nil
}

func TestAcceptedSetupKeepsRunningBootstrapBindingDuringDrain(t *testing.T) {
	r, ctx := boundaryHost(t)
	running := intentFor(r, 1, runtimepb.LifecycleIntent_RUNNING)
	if err := r.ApplyIntent(ctx, running); err != nil {
		t.Fatal(err)
	}
	recorder := &bootstrapBindingRecorder{}
	r.mu.Lock()
	r.runtimeBridge = recorder
	r.mu.Unlock()
	if err := r.ApplyIntent(ctx, intentFor(r, 2, runtimepb.LifecycleIntent_QUIESCING)); err != nil {
		t.Fatal(err)
	}
	if err := r.waitBootstrap(ctx); err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(recorder.binding, running.Binding) {
		t.Fatalf("accepted setup waited using draining binding: %v", recorder.binding)
	}
}

func TestHostPublishesExactIntentAndIdleFacts(t *testing.T) {
	r, ctx := boundaryHost(t)
	running := intentFor(r, 1, runtimepb.LifecycleIntent_RUNNING)
	if err := r.ApplyIntent(ctx, running); err != nil {
		t.Fatal(err)
	}
	read := func(expectedFence bool) lifecycle.HostReport {
		t.Helper()
		result, err := r.lifecycle.state.WatchFor(ctx, lifecycle.HostReportResource(lifecycle.HostReport{}).Metadata(),
			state.WithEventTypes(state.Created, state.Updated),
			state.WithCondition(func(value resource.Resource) (bool, error) {
				report := value.(interface {
					TypedSpec() *lifecycle.HostReport
				}).TypedSpec()
				return report.Intent != nil && report.Intent.ExecutionFenced == expectedFence &&
					proto.Equal(report.Intent.Binding, running.Binding) && report.Report != nil && report.Report.Ready, nil
			}))
		if err != nil {
			t.Fatal(err)
		}
		return *result.(interface {
			TypedSpec() *lifecycle.HostReport
		}).TypedSpec()
	}
	first := read(false)
	if first.Report.Facts == nil || first.Report.Facts.IdleSince == nil || first.Report.Facts.QuiesceStage != 0 {
		t.Fatalf("Host report lost idle and lifecycle facts: %v", first.Report)
	}
	fenced := proto.Clone(running).(*runtimepb.LifecycleIntent)
	fenced.ExecutionFenced = true
	if err := r.ApplyIntent(ctx, fenced); err != nil {
		t.Fatal(err)
	}
	second := read(true)
	if !second.Intent.ExecutionFenced || !proto.Equal(second.Intent.Binding, running.Binding) {
		t.Fatalf("Host report did not bind the same-generation fence: %v", second)
	}
}

func TestAggregateStatusWakesHostReportAndStatusWait(t *testing.T) {
	r, ctx := boundaryHost(t)
	if err := r.ApplyIntent(ctx, intentFor(r, 1, runtimepb.LifecycleIntent_RUNNING)); err != nil {
		t.Fatal(err)
	}
	statusDone := make(chan error, 1)
	go func() { statusDone <- r.lifecycle.wait(ctx, lifecycle.Quiesced) }()
	draining := intentFor(r, 2, runtimepb.LifecycleIntent_QUIESCING)
	if err := r.ApplyIntent(ctx, draining); err != nil {
		t.Fatal(err)
	}
	if err := r.lifecycle.waitFor(ctx, func() (bool, error) {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.nativeStopped, nil
	}); err != nil {
		t.Fatalf("wait for native stop: %v", err)
	}
	draining.ExecutionFenced = true
	if err := r.ApplyIntent(ctx, draining); err != nil {
		t.Fatal(err)
	}
	if err := publishHostIntentStatus(ctx, r, draining, true); err != nil {
		t.Fatal(err)
	}
	result, err := r.lifecycle.state.WatchFor(ctx, lifecycle.HostReportResource(lifecycle.HostReport{}).Metadata(),
		state.WithEventTypes(state.Created, state.Updated),
		state.WithCondition(func(value resource.Resource) (bool, error) {
			report := value.(interface {
				TypedSpec() *lifecycle.HostReport
			}).TypedSpec()
			return report.Intent != nil && report.Intent.Mode == runtimepb.LifecycleIntent_QUIESCING &&
				report.Intent.ExecutionFenced && report.Report != nil && report.Report.Ready &&
				report.Report.Facts.GetQuiesceStage() == environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_QUIESCED, nil
		}))
	if err != nil {
		t.Fatalf("status-only aggregate progress did not reach Host report: %v", err)
	}
	if result == nil {
		t.Fatal("quiesced Host report was absent")
	}
	select {
	case err := <-statusDone:
		if err != nil {
			t.Fatalf("aggregate status wait: %v", err)
		}
	case <-ctx.Done():
		t.Fatal("aggregate status did not wake its waiter")
	}
}

func TestHostReportControllerExitsCleanlyWithHost(t *testing.T) {
	r, ctx := boundaryHost(t)
	intent := &runtimepb.LifecycleIntent{Binding: &runtimepb.Binding{RuntimeBootId: "runtime", Generation: 1}, SandboxUid: "sandbox", Mode: runtimepb.LifecycleIntent_INACTIVE}
	if err := r.ApplyIntent(ctx, intent); err != nil {
		t.Fatal(err)
	}
	if err := r.CloseJournals(ctx); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-r.fatal:
		t.Fatalf("normal Host shutdown reported failure: %v", err)
	default:
	}
}
