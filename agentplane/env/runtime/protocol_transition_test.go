package runtime

import (
	"context"
	"path/filepath"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/proto"
)

func TestLifecycleIntentChangedDuringSendIsDelivered(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	socket := filepath.Join(t.TempDir(), "host.sock")
	r := &Runtime{
		config: Config{HostSocket: socket, Launch: configuration.SandboxLaunch{Sandbox: configuration.SandboxIdentity{UID: "sandbox"}}},
		bootID: "runtime", changes: make(chan struct{}),
		deps: runtimeDeps{dialHost: func(path string) (*grpc.ClientConn, error) {
			return grpc.NewClient("unix://"+path, grpc.WithTransportCredentials(insecure.NewCredentials()))
		}},
	}
	stream := &heldLifecycleStream{ctx: ctx, sent: make(chan *runtimepb.LifecycleIntent, 2), release: make(chan struct{}), requests: make(chan *runtimepb.ConnectRequest, 1)}
	stream.requests <- &runtimepb.ConnectRequest{Message: &runtimepb.ConnectRequest_Hello{Hello: &runtimepb.HostHello{HostBootId: "host", ControlSocket: socket}}}
	done := make(chan error, 1)
	go func() { done <- r.Connect(stream) }()
	t.Cleanup(func() { cancel(); <-done })
	select {
	case <-stream.sent:
	case <-ctx.Done():
		t.Fatal("initial intent was not sent")
	}
	// Publish while the initial Send is held. The next loop must observe pending
	// state even though its notification channel has already been replaced.
	r.mu.Lock()
	want := r.setIntentLocked(runtimepb.LifecycleIntent_RUNNING, "incarnation", "", false)
	r.mu.Unlock()
	close(stream.release)
	select {
	case got := <-stream.sent:
		if !proto.Equal(got, want) {
			t.Fatalf("pending intent = %v, want %v", got, want)
		}
	case <-ctx.Done():
		t.Fatal("intent published during Send was lost")
	}
}

type heldLifecycleStream struct {
	grpc.ServerStream
	ctx      context.Context
	sent     chan *runtimepb.LifecycleIntent
	release  chan struct{}
	requests chan *runtimepb.ConnectRequest
	sends    int
}

func (s *heldLifecycleStream) Context() context.Context { return s.ctx }
func (s *heldLifecycleStream) Recv() (*runtimepb.ConnectRequest, error) {
	select {
	case request := <-s.requests:
		return request, nil
	case <-s.ctx.Done():
		return nil, s.ctx.Err()
	}
}

func (s *heldLifecycleStream) Send(intent *runtimepb.LifecycleIntent) error {
	s.sends++
	select {
	case s.sent <- proto.Clone(intent).(*runtimepb.LifecycleIntent):
	case <-s.ctx.Done():
		return s.ctx.Err()
	}
	if s.sends == 1 {
		select {
		case <-s.release:
		case <-s.ctx.Done():
			return s.ctx.Err()
		}
	}
	return nil
}
