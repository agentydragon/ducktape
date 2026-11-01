package dispatcher

import (
	"context"
	"net"
	"os"
	"path/filepath"
	"testing"
	"time"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/samplerpb"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type openingFixture struct{ opening *Opening }

func (s openingFixture) GetOpening(context.Context, string) (*Opening, error) {
	if s.opening == nil {
		return nil, ErrNotFound
	}
	return s.opening, nil
}

type nativeFixture struct{ calls int }

func (r *nativeFixture) OpenSession(context.Context, SandboxSnapshot, string, *sessionpb.SessionSpec, *string, *delegationpb.ServiceAccountIdentity) error {
	r.calls++
	return nil
}

func (r *nativeFixture) Deliver(context.Context, SandboxSnapshot, string, *commandpb.Command) (*eventlogpb.EventEntry, error) {
	r.calls++
	return &eventlogpb.EventEntry{Cursor: 99}, nil
}

type centralFixture struct {
	samplerpb.UnimplementedSamplerServer
	opened      *samplerpb.OpenSessionRequest
	token       string
	interrupted string
}

func (s *centralFixture) OpenSession(ctx context.Context, request *samplerpb.OpenSessionRequest) (*samplerpb.OpenSessionResponse, error) {
	s.opened = request
	md, _ := metadata.FromIncomingContext(ctx)
	s.token = md.Get("authorization")[0]
	return &samplerpb.OpenSessionResponse{}, nil
}

func (s *centralFixture) InterruptTurn(_ context.Context, request *environmentruntimepb.InterruptTurnRequest) (*environmentruntimepb.InterruptTurnResponse, error) {
	s.interrupted = request.GetTurnId()
	return &environmentruntimepb.InterruptTurnResponse{Admission: &eventlogpb.EventEntry{Cursor: 17}}, nil
}

func TestRoutingKeepsCentralCommandsOffSandboxRunner(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	fixture := &centralFixture{}
	samplerpb.RegisterSamplerServer(server, fixture)
	go server.Serve(listener)
	t.Cleanup(server.Stop)
	tokenFile := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(tokenFile, []byte("projected\n"), 0600); err != nil {
		t.Fatal(err)
	}
	native := &nativeFixture{}
	spec := &sessionpb.SessionSpec{Harness: sessionpb.Harness_HARNESS_CENTRAL_SAMPLER}
	sandbox := SandboxSnapshot{Name: "executor", Namespace: "test", UID: "sandbox-uid", Incarnation: "pod-uid", Blocker: "sandbox_not_ready"}
	runner := &RoutingRunner{Native: native, Store: openingFixture{&Opening{SandboxName: sandbox.Name, SandboxUID: sandbox.UID, Spec: spec}}, Target: &CentralTarget{Target: listener.Addr().String(), TokenFile: tokenFile}, Timeout: time.Second}
	setup := "printf 'executor setup\\n'"
	if err := runner.OpenSession(context.Background(), sandbox, "session", spec, &setup, nil); err != nil {
		t.Fatal(err)
	}
	if fixture.opened.GetExpectedIncarnationUid() != "pod-uid" || fixture.opened.GetExecutor().GetSandboxUid() != "sandbox-uid" || fixture.opened.GetSetupScript() != setup || fixture.token != "Bearer projected" {
		t.Fatalf("unexpected opening: %v token %q", fixture.opened, fixture.token)
	}
	admission, err := runner.Deliver(context.Background(), sandbox, "session", &commandpb.Command{CommandId: "interrupt", Operation: &commandpb.Command_InterruptTurn{InterruptTurn: &commandpb.InterruptTurn{TurnId: "turn"}}})
	if err != nil || admission.GetCursor() != 17 || fixture.interrupted != "turn" || native.calls != 0 {
		t.Fatalf("interrupt %v %v native calls %d", admission, err, native.calls)
	}
	changed := sandbox
	changed.UID = "replacement"
	if _, err := runner.Deliver(context.Background(), changed, "session", &commandpb.Command{}); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("changed binding accepted: %v", err)
	}
	nativeSpec := &sessionpb.SessionSpec{Harness: sessionpb.Harness_HARNESS_CODEX}
	if err := runner.OpenSession(context.Background(), sandbox, "native", nativeSpec, nil, nil); err != nil {
		t.Fatal(err)
	}
	runner.Store = openingFixture{}
	if _, err := runner.Deliver(context.Background(), sandbox, "old-session", &commandpb.Command{}); err != nil {
		t.Fatal(err)
	}
	if native.calls != 2 {
		t.Fatalf("native calls %d", native.calls)
	}
	runner.Target = nil
	if err := runner.OpenSession(context.Background(), sandbox, "disabled", spec, nil, nil); status.Code(err) != codes.FailedPrecondition {
		t.Fatalf("disabled sampler accepted: %v", err)
	}
}
