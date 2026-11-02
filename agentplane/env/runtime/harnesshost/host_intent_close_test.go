package harnesshost

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/cosi"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost/retained"
	harnesshostlifecycle "github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/session_ingresspb"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"google.golang.org/grpc"
)

type connectedHostBridge struct {
	completedBootstrapBridge
	connected chan struct{}
}

func (b *connectedHostBridge) ConnectHost(ctx context.Context, _ *Host) error {
	close(b.connected)
	<-ctx.Done()
	return ctx.Err()
}

type idleHistoryIngress struct {
	ingresspb.UnimplementedSessionJournalIngressServer
}

func (*idleHistoryIngress) Push(stream grpc.BidiStreamingServer[ingresspb.PushFrame, ingresspb.Acknowledgement]) error {
	frame, err := stream.Recv()
	if err != nil {
		return err
	}
	if frame.GetAnnounce() == nil {
		return errors.New("History ingress did not receive a Session announcement")
	}
	if err := stream.Send(&ingresspb.Acknowledgement{}); err != nil {
		return err
	}
	<-stream.Context().Done()
	return nil
}

func waitIntentStatus(t *testing.T, ctx context.Context, store state.State, intent harnesshostlifecycle.HostIntent, done bool) harnesshostlifecycle.HostIntentStatus {
	t.Helper()
	value, err := store.WatchFor(ctx, harnesshostlifecycle.HostIntentStatusResource(harnesshostlifecycle.HostIntentStatus{}).Metadata(),
		state.WithEventTypes(state.Created, state.Updated),
		state.WithCondition(func(value resource.Resource) (bool, error) {
			status := value.(interface {
				TypedSpec() *harnesshostlifecycle.HostIntentStatus
			}).TypedSpec()
			return status.Intent == intent && (status.Applied || status.Error != "") == done, nil
		}))
	if err != nil {
		t.Fatal(err)
	}
	return *value.(interface {
		TypedSpec() *harnesshostlifecycle.HostIntentStatus
	}).TypedSpec()
}

func TestHostRunInterruptedIntentKeepsClosingState(t *testing.T) {
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
	r.SetRuntimeConnection("runtime", r.bootID, true)
	endpoint := loopbackGRPCServer(t, func(server *grpc.Server) {
		ingresspb.RegisterSessionJournalIngressServer(server, &idleHistoryIngress{})
	})
	bridge := &connectedHostBridge{connected: make(chan struct{})}
	runCtx, stop := context.WithCancel(ctx)
	done := make(chan error, 1)
	go func() { done <- r.Run(runCtx, configuration.Endpoint{Target: endpoint}, bridge) }()
	t.Cleanup(func() {
		stop()
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
	select {
	case <-bridge.connected:
	case <-ctx.Done():
		t.Fatal("Host Run did not complete Session startup")
	}
	first := harnesshostlifecycle.HostIntent{RuntimeBootID: "runtime", HostBootID: r.bootID, Generation: 1, SandboxUID: "sandbox", Mode: runtimepb.LifecycleIntent_INACTIVE}
	if err := cosi.Publish(ctx, store, harnesshostlifecycle.HostIntentResource(first), first, harnesshostlifecycle.EnvironmentOwner); err != nil {
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
	if err := cosi.Publish(ctx, store, harnesshostlifecycle.HostIntentResource(next), next, harnesshostlifecycle.EnvironmentOwner); err != nil {
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
	stop()
	r.SetRuntimeConnection("runtime", r.bootID, false)
	select {
	case <-store.closeReadEntered:
	case <-ctx.Done():
		t.Fatal("Host Run did not publish Closing")
	}
	close(store.release)
	if status := waitIntentStatus(t, ctx, store, next, true); status.Applied || status.Error == "" {
		t.Fatalf("interrupted Host intent status = %+v", status)
	}
	if got := r.lifecycle.desiredState().Mode; got != harnesshostlifecycle.Closing {
		t.Fatalf("Host desired state after interrupted intent = %v, want Closing", got)
	}
	close(store.closeReadRelease)
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("Host Run after coordinated close = %v", err)
		}
	case <-ctx.Done():
		t.Fatal("Host Run did not finish")
	}
}
