package runner

import (
	"context"
	"errors"
	"net"
	"testing"
	"time"

	ingresspb "github.com/agentydragon/ducktape/agentplane/history_service/runner_ingresspb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	tunnelpb "github.com/agentydragon/ducktape/agentplane/proto/grpc_tunnel"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	controlpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/proto"
)

func loopbackGRPCServer(t *testing.T, register func(*grpc.Server)) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	register(server)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})
	return listener.Addr().String()
}

type controlAudit struct {
	Authorization string
	Hello         *controlpb.Hello
	Facts         *controlpb.RunnerFacts
	Reply         *tunnelpb.Reply
	Err           error
}

type controlLoopback struct {
	controlpb.UnimplementedRunnerControlServer
	result chan controlAudit
}

func (s *controlLoopback) Connect(stream grpc.BidiStreamingServer[controlpb.RunnerFrame, controlpb.ServiceFrame]) (resultErr error) {
	audit := controlAudit{}
	defer func() {
		audit.Err = resultErr
		s.result <- audit
	}()
	if values, ok := metadata.FromIncomingContext(stream.Context()); ok {
		audit.Authorization = firstString(values.Get("authorization"))
	}
	frame, err := stream.Recv()
	if err != nil {
		return err
	}
	audit.Hello = frame.GetHello()
	if audit.Hello == nil {
		return errors.New("first control frame was not Hello")
	}
	if err := stream.Send(&controlpb.ServiceFrame{Frame: &controlpb.ServiceFrame_Welcome{Welcome: &controlpb.Welcome{ConnectionEpoch: 1, IncarnationUid: "loopback-incarnation"}}}); err != nil {
		return err
	}
	frame, err = stream.Recv()
	if err != nil {
		return err
	}
	if heartbeat := frame.GetHeartbeat(); heartbeat != nil {
		audit.Facts = heartbeat.GetFacts()
	}
	if audit.Facts == nil {
		return errors.New("runner did not publish facts after Welcome")
	}
	request, err := proto.Marshal(&runnerpb.ListSessionsRequest{})
	if err != nil {
		return err
	}
	if err := stream.Send(&controlpb.ServiceFrame{Frame: &controlpb.ServiceFrame_Call{Call: &tunnelpb.Call{
		CallId: 17, Method: "/agentplane.runner.Runner/ListSessions", Request: request,
	}}}); err != nil {
		return err
	}
	frame, err = stream.Recv()
	if err != nil {
		return err
	}
	reply := frame.GetReply()
	if reply == nil {
		return errors.New("runner did not answer tunneled ListSessions call")
	}
	audit.Reply = reply
	return nil
}

func firstString(values []string) string {
	if len(values) == 0 {
		return ""
	}
	return values[0]
}

type quiesceIngress struct {
	ingresspb.UnimplementedRunnerIngressServer
	sealCursor    uint64
	underAckSent  chan struct{}
	releaseSeal   chan struct{}
	authorization chan string
	result        chan error
}

func (s *quiesceIngress) Push(stream grpc.BidiStreamingServer[ingresspb.PushFrame, ingresspb.Acknowledgement]) (resultErr error) {
	defer func() { s.result <- resultErr }()
	if values, ok := metadata.FromIncomingContext(stream.Context()); ok {
		s.authorization <- firstString(values.Get("authorization"))
	}
	frame, err := stream.Recv()
	if err != nil {
		return err
	}
	if announce := frame.GetAnnounce(); announce == nil || announce.GetSessionId() == "" {
		return errors.New("first ingress frame was not a Session announcement")
	}
	if err := stream.Send(&ingresspb.Acknowledgement{Cursor: 0}); err != nil {
		return err
	}
	for cursor := uint64(1); cursor < s.sealCursor; cursor++ {
		frame, err = stream.Recv()
		if err != nil {
			return err
		}
		if entry := frame.GetEntry(); entry == nil || entry.GetCursor() != cursor {
			return errors.New("runner sent a nonsequential EventEntry")
		}
	}
	if err := stream.Send(&ingresspb.Acknowledgement{Cursor: s.sealCursor - 1}); err != nil {
		return err
	}
	close(s.underAckSent)
	select {
	case <-s.releaseSeal:
	case <-stream.Context().Done():
		return stream.Context().Err()
	}
	frame, err = stream.Recv()
	if err != nil {
		return err
	}
	if entry := frame.GetEntry(); entry == nil || entry.GetCursor() != s.sealCursor {
		return errors.New("runner did not send the last EventEntry before the seal")
	}
	frame, err = stream.Recv()
	if err != nil {
		return err
	}
	sealed := frame.GetSealed()
	if sealed == nil || sealed.GetCursor() != s.sealCursor {
		return errors.New("runner sent the seal before the final journal cursor")
	}
	return stream.Send(&ingresspb.Acknowledgement{Cursor: s.sealCursor, Sealed: true})
}

func TestIngressLoopPublishesSealedAcknowledgementToQuiesce(t *testing.T) {
	runner, session := lifecycleTestSession(t)
	ctx, cancel := context.WithTimeout(context.Background(), lifecycleTestTimeout)
	defer cancel()
	if err := session.Emit(ctx, &eventpb.Event{Observation: &eventpb.Event_HarnessStderr{HarnessStderr: &eventpb.HarnessStderr{Text: "retained"}}}, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := runner.Quiesce(ctx, &runnerpb.QuiesceRequest{}); err != nil {
		t.Fatalf("accept quiesce: %v", err)
	}
	waitLifecycle(t, runner, func() bool { return runner.lifecycle.status().Stage == runnerlifecycle.Flushing }, "quiesce to seal the journal")
	seal := session.EventJournal().SealState()
	if seal == nil || seal.Cursor == 0 {
		t.Fatalf("journal seal = %+v, want a nonzero cursor", seal)
	}

	service := &quiesceIngress{
		sealCursor:    seal.Cursor,
		underAckSent:  make(chan struct{}),
		releaseSeal:   make(chan struct{}),
		authorization: make(chan string, 1),
		result:        make(chan error, 1),
	}
	endpoint := loopbackGRPCServer(t, func(server *grpc.Server) {
		ingresspb.RegisterRunnerIngressServer(server, service)
	})
	loopDone := make(chan error, 1)
	go func() { loopDone <- runner.ingressLoop(ctx, Endpoint{Target: endpoint, Bearer: "ingress-token"}) }()

	select {
	case authorization := <-service.authorization:
		if authorization != "Bearer ingress-token" {
			t.Fatalf("ingress authorization = %q", authorization)
		}
	case <-ctx.Done():
		t.Fatalf("ingress call did not start: %v", ctx.Err())
	}
	select {
	case <-service.underAckSent:
	case <-ctx.Done():
		t.Fatalf("ingress did not send its under-cursor acknowledgement: %v", ctx.Err())
	}
	waitLifecycle(t, runner, func() bool {
		ack, found, err := lifecycleRead[ingressAckSpec](ctx, runner.lifecycle.state, session.ID())
		return err == nil && found && ack.Cursor == seal.Cursor-1 && !ack.Sealed
	}, "under-cursor ingress acknowledgement observation")
	if ack, err := runner.store.ReadAcknowledgement(session.ID()); err != nil || ack == nil || ack.Cursor != seal.Cursor-1 || ack.Sealed {
		t.Fatalf("persisted under-cursor acknowledgement = %+v, err %v", ack, err)
	}
	if stage := runner.lifecycle.status().Stage; stage != runnerlifecycle.Flushing {
		t.Fatalf("quiesce accepted under-cursor acknowledgement at stage %v", stage)
	}
	close(service.releaseSeal)
	waitLifecycle(t, runner, func() bool { return runner.lifecycle.status().Stage == runnerlifecycle.Quiesced }, "exact sealed ingress acknowledgement")
	if ack, err := runner.store.ReadAcknowledgement(session.ID()); err != nil || ack == nil || ack.Cursor != seal.Cursor || !ack.Sealed {
		t.Fatalf("persisted sealed acknowledgement = %+v, err %v", ack, err)
	}
	if ack, found, err := lifecycleRead[ingressAckSpec](ctx, runner.lifecycle.state, session.ID()); err != nil || !found || ack.Cursor != seal.Cursor || !ack.Sealed {
		t.Fatalf("COSI sealed acknowledgement = %+v, found %t, err %v", ack, found, err)
	}
	if err := <-service.result; err != nil {
		t.Fatalf("ingress server: %v", err)
	}
	cancel()
	if err := <-loopDone; err != nil {
		t.Fatalf("ingress loop shutdown: %v", err)
	}
}

func TestControlConnectAuthenticatesWelcomesPublishesFactsAndRelaysRPC(t *testing.T) {
	runner := newBareRunner(t)
	service := &controlLoopback{result: make(chan controlAudit, 1)}
	endpoint := loopbackGRPCServer(t, func(server *grpc.Server) {
		controlpb.RegisterRunnerControlServer(server, service)
	})
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	connected := make(chan error, 1)
	go func() {
		_, err := runner.connectControl(ctx, Endpoint{Target: endpoint, Bearer: "loopback-token"})
		connected <- err
	}()
	var audit controlAudit
	select {
	case audit = <-service.result:
	case <-ctx.Done():
		t.Fatalf("control exchange timed out: %v", ctx.Err())
	}
	if audit.Err != nil {
		t.Fatalf("control stream failed: %v", audit.Err)
	}
	if audit.Authorization != "Bearer loopback-token" || audit.Hello == nil || audit.Hello.RunnerBootId != runner.bootID {
		t.Fatalf("control authentication/Hello = authorization %q, hello %v", audit.Authorization, audit.Hello)
	}
	if audit.Facts == nil || audit.Facts.IdleSince == nil || audit.Facts.QuiesceStage != controlpb.RunnerFacts_QUIESCE_STAGE_UNSPECIFIED {
		t.Fatalf("first heartbeat facts = %v", audit.Facts)
	}
	if uid, known := runner.store.IncarnationUID(); !known || uid != "loopback-incarnation" {
		t.Fatalf("Welcome incarnation = %q, %t", uid, known)
	}
	if audit.Reply == nil || audit.Reply.CallId != 17 || audit.Reply.Code != int32(codes.OK) {
		t.Fatalf("tunneled RPC reply = %v", audit.Reply)
	}
	response := &runnerpb.ListSessionsResponse{}
	if err := proto.Unmarshal(audit.Reply.Response, response); err != nil || len(response.Sessions) != 0 {
		t.Fatalf("tunneled ListSessions response = %v, %v", response, err)
	}
	select {
	case err := <-connected:
		if err == nil {
			t.Fatal("control stream ended successfully without cancellation")
		}
	case <-ctx.Done():
		t.Fatalf("control client did not exit after peer closed: %v", ctx.Err())
	}
}

func newBareRunner(t *testing.T) *Runner {
	t.Helper()
	runner, err := New(Config{StateDir: t.TempDir()})
	if err != nil {
		t.Fatal(err)
	}
	if err := runner.store.AdoptIncarnation("test-incarnation"); err != nil {
		_ = runner.owner.Close()
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := runner.Close(); err != nil {
			t.Errorf("close test runner: %v", err)
		}
	})
	return runner
}
