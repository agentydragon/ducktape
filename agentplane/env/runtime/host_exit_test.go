package runtime

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"google.golang.org/protobuf/proto"
)

type observedHostExit struct {
	hostLifetime
	done       chan struct{}
	subscribed chan struct{}
	once       sync.Once
	code       int
	err        error
}

func (h *observedHostExit) Done() <-chan struct{} {
	h.once.Do(func() { close(h.subscribed) })
	return h.done
}

func (h *observedHostExit) Wait(ctx context.Context) (int, error) {
	select {
	case <-h.done:
		return h.code, h.err
	case <-ctx.Done():
		return 0, ctx.Err()
	}
}

func TestInitialHostWaitObservesExitAcrossSubscription(t *testing.T) {
	for _, beforeSubscribe := range []bool{true, false} {
		name := "after-subscribe"
		if beforeSubscribe {
			name = "before-subscribe"
		}
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(t.Context(), time.Second)
			defer cancel()
			failure := errors.New("Host stopped during startup")
			host := &observedHostExit{done: make(chan struct{}), subscribed: make(chan struct{}), code: 1, err: failure}
			r := &Runtime{hostLifetime: host, changes: make(chan struct{})}
			if beforeSubscribe {
				close(host.done)
			}
			result := make(chan error, 1)
			go func() { result <- r.waitForInitialHost(ctx) }()
			select {
			case <-host.subscribed:
			case <-ctx.Done():
				t.Fatal("initial Host wait did not subscribe to lifetime completion")
			}
			if !beforeSubscribe {
				close(host.done)
			}
			select {
			case err := <-result:
				if !errors.Is(err, failure) || !strings.Contains(err.Error(), "status=1") {
					t.Fatalf("initial Host wait = %v, want original exit cause and status", err)
				}
			case <-ctx.Done():
				t.Fatal("Host exit did not wake initial wait")
			}
		})
	}
}

func TestInitialHostWaitReportsCleanUnexpectedExit(t *testing.T) {
	host := &observedHostExit{done: make(chan struct{}), subscribed: make(chan struct{})}
	r := &Runtime{hostLifetime: host, changes: make(chan struct{})}
	close(host.done)
	err := r.waitForInitialHost(t.Context())
	if err == nil || !strings.Contains(err.Error(), "status=0") || strings.Contains(err.Error(), "%!w") {
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
			host := &observedHostExit{done: make(chan struct{}), subscribed: make(chan struct{}), err: errors.New("Host exited")}
			binding := &runtimepb.Binding{RuntimeBootId: "runtime", Generation: 1}
			intent := &runtimepb.LifecycleIntent{Binding: binding, Mode: runtimepb.LifecycleIntent_INACTIVE}
			r := &Runtime{
				hostLifetime: host, host: &hostBinding{active: true}, intent: intent,
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
				close(host.done)
			}
			err := r.waitForInitialHost(ctx)
			if exitBeforeRead {
				if !errors.Is(err, host.err) {
					t.Fatalf("ready report won after Host exit: %v", err)
				}
			} else if err != nil {
				t.Fatalf("ready report before Host exit was rejected: %v", err)
			}
			if !exitBeforeRead {
				close(host.done)
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
			host := &observedHostExit{done: make(chan struct{}), subscribed: make(chan struct{})}
			r := &Runtime{hostLifetime: host, changes: make(chan struct{})}
			result := make(chan error, 1)
			go func() { result <- r.waitForInitialHost(ctx) }()
			select {
			case <-host.subscribed:
			case <-ctx.Done():
				t.Fatal("initial Host wait did not subscribe")
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
	host := &observedHostExit{done: make(chan struct{}), subscribed: make(chan struct{})}
	r.hostLifetime = host
	r.host = &hostBinding{active: true}
	close(host.done)
	coordinator := &runtimeLifecycleController{r: r}
	if err := coordinator.prepare(); err != nil {
		t.Fatal(err)
	}
	defer coordinator.plan.cancelFence()
	if coordinator.plan.active || coordinator.plan.binding != nil || coordinator.plan.host != host {
		t.Fatalf("Close plan after Host exit = %+v, want disconnected Host with cleanup ownership", coordinator.plan)
	}
}
