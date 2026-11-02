package runtime

import (
	"context"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/session"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
)

type heldSuccessorDemand struct {
	state.State
	armed   atomic.Bool
	entered chan struct{}
	release chan struct{}
}

func (s *heldSuccessorDemand) Modify(ctx context.Context, value resource.Resource, update func(resource.Resource) error, opts ...state.UpdateOption) error {
	if err := s.State.Modify(ctx, value, update, opts...); err != nil {
		return err
	}
	if value.Metadata().Type() != session.ReconcileSnapshotType || !s.armed.Load() {
		return nil
	}
	current, found, err := cosi.Read(ctx, s.State, lifecycle.HostIntentStatusResource(lifecycle.HostIntentStatus{}))
	if err != nil {
		return err
	}
	if found && current.Intent.IncarnationUID == "successor" && current.Intent.Mode == runtimepb.LifecycleIntent_RUNNING && !current.Applied && current.Error == "" && s.armed.CompareAndSwap(true, false) {
		close(s.entered)
		select {
		case <-s.release:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	return nil
}

func TestRuntimeCloseWaitsForApplyingHostIntentAndExecutionFence(t *testing.T) {
	store := &heldSuccessorDemand{State: state.WrapCore(namespaced.NewState(inmem.Build)), entered: make(chan struct{}), release: make(chan struct{})}
	proxy := modelConfigServer(t)
	workspace := t.TempDir()
	config := lifecycleConfig(t, proxy.URL, workspace, nil, "/bin/false")
	config.CodexExecServer = &codexexecserver.Config{URL: "ws://unused"}
	config.CodexBinary = "/bin/false"
	config.HarnessEnvironment = map[string]string{}
	successor := &heldCloseExecution{started: make(chan struct{}), release: make(chan struct{})}
	config.deps.startExecution = func(_ context.Context, _ Config, uid string) (executionHandle, error) {
		if uid == "successor" {
			return successor, nil
		}
		return newControllerExecutionHandle(nil), nil
	}
	r := newLifecycleRunnerWithState(t, config, store)
	var releaseDemand, releaseFence sync.Once
	t.Cleanup(func() {
		releaseDemand.Do(func() { close(store.release) })
		releaseFence.Do(func() { close(successor.release) })
	})
	client := startDirectExecutionTestClient(t, r, "test-incarnation")
	if _, err := client.runner.OpenSession(client.callCtx, openLifecycleSession("session-1", filepath.Join(workspace, "project"))); err != nil {
		t.Fatal(err)
	}
	store.armed.Store(true)
	adoptionDone := make(chan error, 1)
	go func() { adoptionDone <- r.adoptIncarnation(context.Background(), "successor") }()
	select {
	case <-store.entered:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("successor Host intent did not reach Session demand reset")
	}
	closeDone := make(chan error, 1)
	go func() { closeDone <- r.Close(context.Background()) }()
	waitRuntime(t, r, func() bool {
		status, found, err := lifecycle.ReadRuntimeCloseStatus(context.Background(), r.lifecycleState)
		return err == nil && found && status.Stage == lifecycle.RuntimeCloseNativeStopping
	}, "Close to wait for the Host native-stop barrier")
	r.mu.Lock()
	closingIntent := r.intent
	if closingIntent == nil || closingIntent.Mode != runtimepb.LifecycleIntent_CLOSING || closingIntent.IncarnationUid != "successor" {
		r.mu.Unlock()
		t.Fatalf("Runtime Close intent = %v, want successor closing intent", closingIntent)
	}
	closeGeneration := closingIntent.Binding.Generation
	r.mu.Unlock()
	applying, found, err := cosi.Read(context.Background(), r.lifecycleState, lifecycle.HostIntentStatusResource(lifecycle.HostIntentStatus{}))
	if err != nil || !found || applying.Intent.IncarnationUID != "successor" || applying.Intent.Generation >= closeGeneration || applying.Applied || applying.Error != "" {
		t.Fatalf("Host status while Close waits = %+v, found=%t, err=%v; want older Applying intent", applying, found, err)
	}
	select {
	case err := <-closeDone:
		t.Fatalf("Close returned before the applying Host intent settled: %v", err)
	default:
	}
	if successor.fences.Load() != 0 {
		t.Fatal("Execution fence started before the Host acknowledged Close")
	}
	if owner, err := openEnvironmentOwner(config.StateDir); err == nil {
		_ = owner.Close()
		t.Fatal("environment owner released while Host intent was applying")
	}
	releaseDemand.Do(func() { close(store.release) })
	select {
	case <-successor.started:
	case <-time.After(lifecycleTestTimeout):
		t.Fatal("successor Execution fence did not start")
	}
	select {
	case err := <-closeDone:
		t.Fatalf("Close returned before Execution fence completed: %v", err)
	default:
	}
	if owner, err := openEnvironmentOwner(config.StateDir); err == nil {
		_ = owner.Close()
		t.Fatal("environment owner released before Execution fence")
	}
	releaseFence.Do(func() { close(successor.release) })
	if err := waitClose(t, closeDone); err != nil {
		t.Fatalf("Close after intent and fence settled: %v", err)
	}
	if err := waitClose(t, adoptionDone); err == nil {
		t.Fatal("superseded adoption succeeded during Close")
	}
	if got := successor.fences.Load(); got != 1 {
		t.Fatalf("successor Execution fence attempts = %d, want 1", got)
	}
	owner, err := openEnvironmentOwner(config.StateDir)
	if err != nil {
		t.Fatalf("environment owner retained after successful Close: %v", err)
	}
	if err := owner.Close(); err != nil {
		t.Fatal(err)
	}
}
