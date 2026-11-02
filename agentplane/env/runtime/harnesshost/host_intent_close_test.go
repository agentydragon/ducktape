package harnesshost

import (
	"context"
	"errors"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	lifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
)

type failedStartupState struct {
	state.State
	armed atomic.Bool
	err   error
}

func (s *failedStartupState) List(ctx context.Context, kind resource.Kind, opts ...state.ListOption) (resource.List, error) {
	if s.armed.Load() && kind.Type() == retained.SessionRecordType {
		return resource.List{}, s.err
	}
	return s.State.List(ctx, kind, opts...)
}

func TestHostStartupFailureFencesAdmission(t *testing.T) {
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	startupErr := errors.New("injected Session recovery failure")
	store := &failedStartupState{State: state.WrapCore(namespaced.NewState(inmem.Build)), err: startupErr}
	r, err := newTestHost(Config{StateDir: t.TempDir()}, store)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { r.cancel(); <-r.lifecycle.done.Done() })
	r.SetRuntimeBinding("runtime", true)
	store.armed.Store(true)
	err = r.Startup(completedBootstrapBridge{})
	if !errors.Is(err, startupErr) {
		t.Fatalf("Host startup = %v, want recovery error", err)
	}
	select {
	case err := <-r.Failures():
		if !errors.Is(err, startupErr) {
			t.Fatalf("reported startup failure = %v", err)
		}
	case <-ctx.Done():
		t.Fatal("Host startup failure was not reported")
	}
	if r.RuntimeBindingMatches("runtime") {
		t.Fatal("failed Host still admits the Runtime binding")
	}
	store.armed.Store(false)
}

func waitIntentStatus(t *testing.T, ctx context.Context, store state.State, intent lifecycle.HostIntent, done bool) lifecycle.HostIntentStatus {
	t.Helper()
	value, err := store.WatchFor(ctx, lifecycle.HostIntentStatusResource(lifecycle.HostIntentStatus{}).Metadata(),
		state.WithEventTypes(state.Created, state.Updated),
		state.WithCondition(func(value resource.Resource) (bool, error) {
			status := value.(interface {
				TypedSpec() *lifecycle.HostIntentStatus
			}).TypedSpec()
			return status.Intent == intent && (status.Applied || status.Error != "") == done, nil
		}))
	if err != nil {
		t.Fatal(err)
	}
	return *value.(interface {
		TypedSpec() *lifecycle.HostIntentStatus
	}).TypedSpec()
}

func TestHostBeginCloseInterruptedIntentKeepsClosingState(t *testing.T) {
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
	r.SetRuntimeBinding("runtime", true)
	if err := r.Startup(completedBootstrapBridge{}); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		select {
		case <-store.release:
		default:
			close(store.release)
		}
		select {
		case <-store.closeReadRelease:
		default:
			close(store.closeReadRelease)
		}
	})
	first := lifecycle.HostIntent{RuntimeBootID: "runtime", Generation: 1, SandboxUID: "sandbox", Mode: runtimepb.LifecycleIntent_INACTIVE}
	if err := cosi.Publish(ctx, store, lifecycle.HostIntentResource(first), first, lifecycle.EnvironmentOwner); err != nil {
		t.Fatal(err)
	}
	if status := waitIntentStatus(t, ctx, store, first, true); !status.Applied {
		t.Fatalf("initial Host intent status = %+v", status)
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
	next := first
	next.Generation++
	next.IncarnationUID = "incarnation"
	next.Mode = runtimepb.LifecycleIntent_RUNNING
	if err := cosi.Publish(ctx, store, lifecycle.HostIntentResource(next), next, lifecycle.EnvironmentOwner); err != nil {
		t.Fatal(err)
	}
	select {
	case <-store.entered:
	case <-ctx.Done():
		t.Fatal("Host intent did not reach Session demand reset")
	}
	if status := waitIntentStatus(t, ctx, store, next, false); status.Applied || status.Error != "" {
		t.Fatalf("Host intent was not Applying before native effect: %+v", status)
	}
	store.closeReadArmed.Store(true)
	done := make(chan error, 1)
	go func() { done <- r.BeginClose(ctx) }()
	r.SetRuntimeBinding("runtime", false)
	select {
	case <-store.closeReadEntered:
	case <-ctx.Done():
		t.Fatal("Host did not publish Closing")
	}
	close(store.release)
	if status := waitIntentStatus(t, ctx, store, next, true); status.Applied || status.Error == "" {
		t.Fatalf("interrupted Host intent status = %+v", status)
	}
	if got := r.lifecycle.desiredState().Mode; got != lifecycle.Closing {
		t.Fatalf("Host desired state after interrupted intent = %v, want Closing", got)
	}
	close(store.closeReadRelease)
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Host BeginClose after coordinated close = %v", err)
		}
	case <-ctx.Done():
		t.Fatal("Host BeginClose did not finish")
	}
}
