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

func TestInitialHostWaitObservesExitAcrossSubscription(t *testing.T) {
	for _, beforeSubscribe := range []bool{true, false} {
		name := "after-subscribe"
		if beforeSubscribe {
			name = "before-subscribe"
		}
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(t.Context(), time.Second)
			defer cancel()
			failure := errors.New("Session worker stopped during startup")
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
				t.Fatal("initial Session wait did not observe worker completion")
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
				t.Fatal("Host exit did not wake initial wait")
			}
		})
	}
}

func TestInitialHostWaitReportsCleanUnexpectedExit(t *testing.T) {
	r := &Runtime{sessionWorkerDone: make(chan struct{}), changes: make(chan struct{})}
	close(r.sessionWorkerDone)
	err := r.waitForInitialHost(t.Context())
	if err == nil || !strings.Contains(err.Error(), "Session worker stopped") {
		t.Fatalf("clean unexpected Host exit = %v", err)
	}
}

func TestInitialHostWaitOrdersReadyReportAndExit(t *testing.T) {
	for _, exitBeforeRead := range []bool{true, false} {
		name := "ready-report-first"
		if exitBeforeRead {
			name = "exit-before-ready-read"
		}
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(t.Context(), time.Second)
			defer cancel()
			workerErr := errors.New("Session worker exited")
			binding := &runtimepb.Binding{RuntimeBootId: "runtime", Generation: 1}
			intent := &runtimepb.LifecycleIntent{Binding: binding, Mode: runtimepb.LifecycleIntent_INACTIVE}
			r := &Runtime{
				sessionWorkerDone: make(chan struct{}), sessionWorkerErr: workerErr,
				host: &hostBinding{active: true}, intent: intent,
				lifecycleState: state.WrapCore(namespaced.NewState(inmem.Build)), changes: make(chan struct{}), fatal: make(chan error, 1),
			}
			publishAppliedHostIntent(t, r)
			report := lifecycle.HostReport{Intent: proto.Clone(intent).(*runtimepb.LifecycleIntent), Report: &runtimepb.HostReport{
				Ready: true, NativeStopped: true,
			}}
			if err := cosi.Publish(ctx, r.lifecycleState, lifecycle.HostReportResource(report), report, "harness-host-report"); err != nil {
				t.Fatal(err)
			}
			if exitBeforeRead {
				close(r.sessionWorkerDone)
			}
			err := r.waitForInitialHost(ctx)
			if exitBeforeRead {
				if !errors.Is(err, workerErr) {
					t.Fatalf("ready report won after Host exit: %v", err)
				}
			} else if err != nil {
				t.Fatalf("ready report before Host exit was rejected: %v", err)
			}
			if !exitBeforeRead {
				close(r.sessionWorkerDone)
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
			r.changedLocked()
			r.mu.Unlock()
			select {
			case err := <-result:
				if err == nil || !strings.Contains(err.Error(), "failed or closed") {
					t.Fatalf("initial Host wait after Runtime %s = %v", name, err)
				}
			case <-ctx.Done():
				t.Fatalf("Runtime %s did not wake initial Host wait", name)
			}
		})
	}
}

func TestClosePreparationTreatsExitedHostAsDisconnected(t *testing.T) {
	config := lifecycleConfig(t, "", t.TempDir(), nil, "/bin/false")
	r, err := newRuntime(config.Config, nil, runtimeDeps{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = r.owner.Close() })
	r.sessionWorkerDone = make(chan struct{})
	r.sessions = &harnesshost.Host{}
	r.host = &hostBinding{active: true}
	close(r.sessionWorkerDone)
	coordinator := &runtimeLifecycleController{r: r}
	if err := coordinator.prepare(); err != nil {
		t.Fatal(err)
	}
	defer coordinator.plan.cancelFence()
	if coordinator.plan.active || coordinator.plan.binding != nil || !coordinator.plan.sessionsStarted {
		t.Fatalf("Close plan after Host exit = %+v, want disconnected Host with cleanup ownership", coordinator.plan)
	}
}
