package runtime

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/bootstrap"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/cosistate"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/retained"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
)

func TestBootstrapIsActiveUntilAcceptedRequestIsTerminal(t *testing.T) {
	for _, tc := range []struct {
		name         string
		request      bootstrap.Request
		requestFound bool
		status       bootstrap.Status
		statusFound  bool
		wantActive   bool
	}{
		{name: "not requested", requestFound: true, status: bootstrap.Status{Phase: bootstrap.Dormant}, statusFound: true},
		{name: "accepted before controller runs", request: bootstrap.Request{Requested: true}, requestFound: true, wantActive: true},
		{name: "running", request: bootstrap.Request{Requested: true}, requestFound: true, status: bootstrap.Status{Phase: bootstrap.Running}, statusFound: true, wantActive: true},
		{name: "succeeded", request: bootstrap.Request{Requested: true}, requestFound: true, status: bootstrap.Status{Phase: bootstrap.Succeeded}, statusFound: true},
		{name: "failed", request: bootstrap.Request{Requested: true}, requestFound: true, status: bootstrap.Status{Phase: bootstrap.Failed}, statusFound: true},
		{name: "interrupted", request: bootstrap.Request{Requested: true}, requestFound: true, status: bootstrap.Status{Phase: bootstrap.Interrupted}, statusFound: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			if got := bootstrapIsActive(tc.request, tc.requestFound, tc.status, tc.statusFound); got != tc.wantActive {
				t.Fatalf("bootstrapIsActive() = %t, want %t", got, tc.wantActive)
			}
		})
	}
}

func TestCanceledBootstrapWaiterDoesNotCancelWorkAndCloseWaits(t *testing.T) {
	stateDir := t.TempDir()
	workspace := t.TempDir()
	started := filepath.Join(stateDir, "bootstrap-started")
	release := filepath.Join(stateDir, "bootstrap-release")
	count := filepath.Join(stateDir, "bootstrap-count")
	script := fmt.Sprintf(": > %s\nwhile [ ! -f %s ]; do sleep 0.01; done\nprintf b >> %s", shellQuote(started), shellQuote(release), shellQuote(count))
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, workspace, &script, "/bin/false")
	config.StateDir = stateDir
	r := newLifecycleRuntime(t, config)
	t.Cleanup(func() { _ = os.WriteFile(release, nil, 0o600) })

	waitForFile(t, started)
	facts, err := r.facts()
	if err != nil {
		t.Fatal(err)
	}
	if facts.Bootstrap != environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_RUNNING || !hasAutosuspendReason(facts.AutosuspendBlockers, "Bootstrap") || facts.IdleSince != nil {
		t.Fatalf("running bootstrap facts = %+v, want Bootstrap blocker and no idle timestamp", facts)
	}
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if err := r.bootstrapHost.Wait(ctx); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled bootstrap waiter error = %v, want context.Canceled", err)
	}

	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close(context.Background()) }()
	waitRuntime(t, r, r.isClosing, "Runtime close request")
	callerCtx, cancelCaller := context.WithCancel(context.Background())
	cancelCaller()
	if err := r.Close(callerCtx); !errors.Is(err, context.Canceled) {
		t.Fatalf("canceled Close waiter = %v, want context cancellation", err)
	}
	waitRuntime(t, r, func() bool {
		r.mu.Lock()
		defer r.mu.Unlock()
		return r.desired.Mode == lifecycle.Closing
	}, "published Runtime closing generation")
	r.mu.Lock()
	closeGeneration := r.desired.Generation
	r.mu.Unlock()
	waitRuntime(t, r, func() bool {
		status, found, err := lifecycle.ReadRuntimeCloseStatus(context.Background(), r.lifecycleState)
		return err == nil && found && status.Generation == closeGeneration
	}, "generation-bound Runtime Close status")
	status, _, err := lifecycle.ReadRuntimeCloseStatus(context.Background(), r.lifecycleState)
	if err != nil || status.Stage == lifecycle.RuntimeCloseReadyToJoin {
		t.Fatalf("Close status during accepted bootstrap = %+v, %v", status, err)
	}
	select {
	case err := <-closeDone:
		t.Fatalf("Close returned while the accepted bootstrap was running: %v", err)
	default:
	}
	if err := os.WriteFile(release, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := waitClose(t, closeDone); err != nil {
		t.Fatal(err)
	}

	if data, err := os.ReadFile(count); err != nil || string(data) != "b" {
		t.Fatalf("bootstrap effect after close = %q, %v; want one execution", data, err)
	}

	restarted := newLifecycleRuntime(t, config)
	ctx, cancel = context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	waitRuntime(t, restarted, func() bool { settled, err := restarted.bootstrapHost.Settled(ctx); return err == nil && settled }, "bootstrap settlement")
	if err := restarted.Close(context.Background()); err != nil {
		t.Fatal(err)
	}
	if data, err := os.ReadFile(count); err != nil || string(data) != "b" {
		t.Fatalf("bootstrap effect after restart = %q, %v; want one execution", data, err)
	}
}

func TestRuntimeCloseEngineCancelJoinsBootstrapWaitAndRetainsOwner(t *testing.T) {
	stateDir := t.TempDir()
	started := filepath.Join(stateDir, "bootstrap-started")
	release := filepath.Join(stateDir, "bootstrap-release")
	script := fmt.Sprintf(": > %s\nwhile [ ! -f %s ]; do sleep 0.01; done", shellQuote(started), shellQuote(release))
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, t.TempDir(), &script, "/bin/false")
	config.StateDir = stateDir
	config.expectCloseError = true
	r := newLifecycleRuntime(t, config)
	t.Cleanup(func() { _ = os.WriteFile(release, nil, 0o600) })
	waitForFile(t, started)
	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close(context.Background()) }()
	waitRuntime(t, r, func() bool {
		status, found, err := lifecycle.ReadRuntimeCloseStatus(context.Background(), r.lifecycleState)
		return err == nil && found && status.Stage == lifecycle.RuntimeCloseBootstrapSettling
	}, "Runtime bootstrap settlement barrier")
	r.lifecycleCancel()
	select {
	case <-r.lifecycleDone:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("Close worker held the canceled COSI engine waiting for bootstrap status")
	}
	if err := waitClose(t, closeDone); err == nil {
		t.Fatal("Close succeeded after its lifecycle engine was canceled")
	}
	if owner, err := openEnvironmentOwner(stateDir); err == nil {
		_ = owner.Close()
		t.Fatal("engine cancellation released environment owner")
	}
}

type bootstrapEventWriteFailureState struct {
	state.State
	fail atomic.Bool
	err  error
}

func (s *bootstrapEventWriteFailureState) Create(ctx context.Context, value resource.Resource, opts ...state.CreateOption) error {
	if s.fail.Load() && value.Metadata().Type() == bootstrap.EventType {
		return s.err
	}
	return s.State.Create(ctx, value, opts...)
}

func TestBootstrapEventStorageFailureDoesNotLeaveBusyFact(t *testing.T) {
	stateDir := t.TempDir()
	workspace := t.TempDir()
	started := filepath.Join(stateDir, "bootstrap-started")
	release := filepath.Join(stateDir, "bootstrap-release")
	storageErr := errors.New("bootstrap event storage failed")
	script := fmt.Sprintf(": > %s\nwhile [ ! -f %s ]; do sleep 0.01; done\nprintf output", shellQuote(started), shellQuote(release))
	proxy := modelConfigServer(t)
	config := lifecycleConfig(t, proxy.URL, workspace, &script, "/bin/false")
	config.StateDir = stateDir
	resources, closer, err := cosistate.Open(stateDir, lifecycle.Namespace, bootstrap.SelectionRegistration(), bootstrap.EventRegistration(), retained.SessionRecordRegistration(), retained.IngressAckRegistration())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := closer.Close(); err != nil {
			t.Errorf("close bootstrap event store: %v", err)
		}
	})
	eventState := &bootstrapEventWriteFailureState{State: resources, err: storageErr}
	r := newLifecycleRunnerWithState(t, config, eventState)
	t.Cleanup(func() { _ = os.WriteFile(release, nil, 0o600) })
	waitForFile(t, started)

	eventState.fail.Store(true)
	if err := os.WriteFile(release, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	waitRuntime(t, r, func() bool { settled, err := r.bootstrapHost.Settled(ctx); return err == nil && settled }, "failed bootstrap settlement")

	facts, err := r.facts()
	if err != nil {
		t.Fatal(err)
	}
	if facts.Bootstrap != environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_INTERRUPTED {
		t.Fatalf("bootstrap fact = %v, want interrupted", facts.Bootstrap)
	}
	for _, blocker := range facts.AutosuspendBlockers {
		if blocker.GetReason() == "Bootstrap" {
			t.Fatal("terminal bootstrap resource still blocks autosuspend")
		}
	}
	status, found, err := bootstrap.ReadStatus(ctx, r.lifecycleState)
	if err != nil || !found || status.Phase != bootstrap.Interrupted || !strings.Contains(status.Error, storageErr.Error()) {
		t.Fatalf("bootstrap status after event write failure = (%+v, found=%t, err=%v), want interrupted with storage failure", status, found, err)
	}
}
