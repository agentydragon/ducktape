package runtime

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"google.golang.org/protobuf/proto"
)

func TestInitialHostWaitObservesRecoveryFailureAcrossSubscription(t *testing.T) {
	for _, beforeSubscribe := range []bool{true, false} {
		name := "after-subscribe"
		if beforeSubscribe {
			name = "before-subscribe"
		}
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(t.Context(), time.Second)
			defer cancel()
			failure := errors.New("Session recovery failed")
			r := &Runtime{sessionWorkerDone: make(chan struct{}), sessionWorkerErr: failure, changes: make(chan struct{})}
			subscribed := make(chan struct{})
			var observed sync.Once
			r.deps.afterInitialWaitSnapshot = func() { observed.Do(func() { close(subscribed) }) }
			if beforeSubscribe {
				close(r.sessionWorkerDone)
			}
			result := make(chan error, 1)
			go func() { result <- r.waitForInitialHost(ctx) }()
			select {
			case <-subscribed:
			case <-ctx.Done():
				t.Fatal("initial Session wait did not observe recovery completion")
			}
			if !beforeSubscribe {
				close(r.sessionWorkerDone)
			}
			select {
			case err := <-result:
				if !errors.Is(err, failure) {
					t.Fatalf("initial Session wait = %v, want original worker error", err)
				}
			case <-ctx.Done():
				t.Fatal("Session recovery failure did not wake initial wait")
			}
		})
	}
}

func TestInitialHostWaitRequiresRecoveryEvenWithReadyReport(t *testing.T) {
	for _, recoveryFails := range []bool{true, false} {
		name := "successful-recovery"
		if recoveryFails {
			name = "failed-recovery"
		}
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(t.Context(), time.Second)
			defer cancel()
			recoveryErr := errors.New("Session recovery failed")
			binding := &runtimepb.Binding{RuntimeBootId: "runtime", Generation: 1}
			intent := &runtimepb.LifecycleIntent{Binding: binding, Mode: runtimepb.LifecycleIntent_INACTIVE}
			r := &Runtime{
				sessionWorkerDone: make(chan struct{}),
				host:              &hostBinding{active: true}, intent: intent,
				lifecycleState: state.WrapCore(namespaced.NewState(inmem.Build)), changes: make(chan struct{}), fatal: make(chan error, 1),
			}
			if recoveryFails {
				r.sessionWorkerErr = recoveryErr
			}
			publishAppliedHostIntent(t, r)
			report := lifecycle.HostReport{Intent: proto.Clone(intent).(*runtimepb.LifecycleIntent), Report: &runtimepb.HostReport{
				Ready: true, NativeStopped: true,
			}}
			if err := cosi.Publish(ctx, r.lifecycleState, lifecycle.HostReportResource(report), report, "harness-host-report"); err != nil {
				t.Fatal(err)
			}
			if recoveryFails {
				close(r.sessionWorkerDone)
				if err := r.waitForInitialHost(ctx); !errors.Is(err, recoveryErr) {
					t.Fatalf("ready report won over failed recovery: %v", err)
				}
				return
			}
			subscribed := make(chan struct{})
			var observed sync.Once
			r.deps.afterInitialWaitSnapshot = func() { observed.Do(func() { close(subscribed) }) }
			result := make(chan error, 1)
			go func() { result <- r.waitForInitialHost(ctx) }()
			select {
			case <-subscribed:
			case <-ctx.Done():
				t.Fatal("initial Host wait did not inspect the ready report")
			}
			select {
			case err := <-result:
				t.Fatalf("ready report bypassed Session recovery: %v", err)
			default:
			}
			close(r.sessionWorkerDone)
			select {
			case err := <-result:
				if err != nil {
					t.Fatalf("ready report after Session recovery = %v", err)
				}
			case <-ctx.Done():
				t.Fatal("successful Session recovery did not wake initial Host wait")
			}
		})
	}
}

func TestInitialHostWaitWakesOnRuntimeFailureOrClose(t *testing.T) {
	for _, closing := range []bool{false, true} {
		name := "failure"
		if closing {
			name = "close"
		}
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(t.Context(), time.Second)
			defer cancel()
			failure := errors.New("injected Runtime failure")
			r := &Runtime{sessionWorkerDone: make(chan struct{}), changes: make(chan struct{})}
			subscribed := make(chan struct{})
			var observed sync.Once
			r.deps.afterInitialWaitSnapshot = func() { observed.Do(func() { close(subscribed) }) }
			result := make(chan error, 1)
			go func() { result <- r.waitForInitialHost(ctx) }()
			select {
			case <-subscribed:
			case <-ctx.Done():
				t.Fatal("initial Session wait did not observe Runtime state")
			}
			r.mu.Lock()
			r.failed = !closing
			r.closing = closing
			if !closing {
				r.failureErr = failure
			}
			r.changedLocked()
			r.mu.Unlock()
			select {
			case err := <-result:
				if !closing && !errors.Is(err, failure) {
					t.Fatalf("initial Host wait after Runtime failure = %v, want injected cause", err)
				}
				if closing && (err == nil || !strings.Contains(err.Error(), "closed")) {
					t.Fatalf("initial Host wait after Runtime %s = %v", name, err)
				}
			case <-ctx.Done():
				t.Fatalf("Runtime %s did not wake initial Host wait", name)
			}
		})
	}
}

func TestClosePreparationTreatsFailedRecoveryAsDisconnected(t *testing.T) {
	config := lifecycleConfig(t, "", t.TempDir(), nil, "/bin/false")
	r, err := newRuntime(config.Config, nil, runtimeDeps{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = r.owner.Close() })
	r.sessionWorkerDone = make(chan struct{})
	r.sessionWorkerErr = errors.New("Session recovery failed")
	r.sessions = &harnesshost.Host{}
	r.host = &hostBinding{active: true}
	close(r.sessionWorkerDone)
	coordinator := &runtimeLifecycleController{r: r}
	if err := coordinator.prepare(); err != nil {
		t.Fatal(err)
	}
	defer coordinator.plan.cancelFence()
	if coordinator.plan.active || coordinator.plan.binding != nil || !coordinator.plan.sessionsStarted {
		t.Fatalf("Close plan after failed Session recovery = %+v, want disconnected Session component with cleanup ownership", coordinator.plan)
	}
}
