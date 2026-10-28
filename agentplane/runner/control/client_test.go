package control

import (
	"context"
	"errors"
	"net"
	"sync/atomic"
	"testing"
	"time"

	tunnelpb "github.com/agentydragon/ducktape/agentplane/proto/grpc_tunnel"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	controlpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type runnerService struct {
	runnerservicepb.UnimplementedRunnerServer
	listSessions func(context.Context, *runnerpb.ListSessionsRequest) (*runnerpb.ListSessionsResponse, error)
}

func (s *runnerService) ListSessions(ctx context.Context, request *runnerpb.ListSessionsRequest) (*runnerpb.ListSessionsResponse, error) {
	return s.listSessions(ctx, request)
}

type controlService struct {
	controlpb.UnimplementedRunnerControlServer
	connect func(grpc.BidiStreamingServer[controlpb.RunnerFrame, controlpb.ServiceFrame]) error
}

func (s *controlService) Connect(stream grpc.BidiStreamingServer[controlpb.RunnerFrame, controlpb.ServiceFrame]) error {
	return s.connect(stream)
}

func controlServer(t *testing.T, service *controlService) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	controlpb.RegisterRunnerControlServer(server, service)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})
	return listener.Addr().String()
}

func controlClient(t *testing.T, service runnerservicepb.RunnerServer, facts func() (*controlpb.RunnerFacts, error), adopt func(context.Context, string) error, stopRetry func(error) bool) *Client {
	t.Helper()
	client, err := New(Dependencies{
		BootID:           "test-boot-id",
		Server:           service,
		AdoptIncarnation: adopt,
		Facts:            facts,
		Changes:          func() <-chan struct{} { return nil },
		StopRetry:        stopRetry,
	})
	if err != nil {
		t.Fatal(err)
	}
	return client
}

func welcome(stream grpc.BidiStreamingServer[controlpb.RunnerFrame, controlpb.ServiceFrame]) error {
	frame, err := stream.Recv()
	if err != nil {
		return err
	}
	if hello := frame.GetHello(); hello == nil || hello.RunnerBootId != "test-boot-id" {
		return errors.New("first control frame was not the expected Hello")
	}
	return stream.Send(&controlpb.ServiceFrame{Frame: &controlpb.ServiceFrame_Welcome{Welcome: &controlpb.Welcome{ConnectionEpoch: 1, IncarnationUid: "loopback-incarnation"}}})
}

func receiveFacts(stream grpc.BidiStreamingServer[controlpb.RunnerFrame, controlpb.ServiceFrame]) (*controlpb.RunnerFacts, error) {
	frame, err := stream.Recv()
	if err != nil {
		return nil, err
	}
	if heartbeat := frame.GetHeartbeat(); heartbeat != nil && heartbeat.GetFacts() != nil {
		return heartbeat.GetFacts(), nil
	}
	return nil, errors.New("runner did not publish facts after Welcome")
}

func TestClientAuthenticatesWelcomesPublishesFactsAndRelaysRPC(t *testing.T) {
	type audit struct {
		authorization string
		hello         *controlpb.Hello
		facts         *controlpb.RunnerFacts
		reply         *tunnelpb.Reply
	}
	audited := make(chan audit, 1)
	service := &controlService{connect: func(stream grpc.BidiStreamingServer[controlpb.RunnerFrame, controlpb.ServiceFrame]) error {
		values, _ := metadata.FromIncomingContext(stream.Context())
		frame, err := stream.Recv()
		if err != nil {
			return err
		}
		current := audit{authorization: first(values.Get("authorization")), hello: frame.GetHello()}
		if err := stream.Send(&controlpb.ServiceFrame{Frame: &controlpb.ServiceFrame_Welcome{Welcome: &controlpb.Welcome{ConnectionEpoch: 1, IncarnationUid: "loopback-incarnation"}}}); err != nil {
			return err
		}
		current.facts, err = receiveFacts(stream)
		if err != nil {
			return err
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
		current.reply = frame.GetReply()
		audited <- current
		return nil
	}}
	endpoint := controlServer(t, service)
	var adopted string
	facts := &controlpb.RunnerFacts{IdleSince: timestamppb.Now()}
	rpc := &runnerService{listSessions: func(context.Context, *runnerpb.ListSessionsRequest) (*runnerpb.ListSessionsResponse, error) {
		return &runnerpb.ListSessionsResponse{}, nil
	}}
	client := controlClient(t, rpc, func() (*controlpb.RunnerFacts, error) {
		return facts, nil
	}, func(_ context.Context, uid string) error {
		adopted = uid
		return nil
	}, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	connected := make(chan error, 1)
	go func() {
		_, err := client.connect(ctx, Endpoint{Target: endpoint, Bearer: "loopback-token"})
		connected <- err
	}()
	current := <-audited
	if current.authorization != "Bearer loopback-token" || current.hello == nil || current.hello.RunnerBootId != "test-boot-id" {
		t.Fatalf("control authentication/Hello = %q, %v", current.authorization, current.hello)
	}
	if current.facts == nil || current.facts.IdleSince == nil || current.facts.QuiesceStage != controlpb.RunnerFacts_QUIESCE_STAGE_UNSPECIFIED {
		t.Fatalf("first heartbeat facts = %v", current.facts)
	}
	if adopted != "loopback-incarnation" {
		t.Fatalf("adopted incarnation = %q", adopted)
	}
	if current.reply == nil || current.reply.CallId != 17 || current.reply.Code != int32(codes.OK) {
		t.Fatalf("tunneled RPC reply = %v", current.reply)
	}
	response := &runnerpb.ListSessionsResponse{}
	if err := proto.Unmarshal(current.reply.Response, response); err != nil || len(response.Sessions) != 0 {
		t.Fatalf("tunneled ListSessions response = %v, %v", response, err)
	}
	select {
	case result := <-connected:
		if result == nil {
			t.Fatal("control stream ended successfully without cancellation")
		}
	case <-ctx.Done():
		t.Fatalf("control client did not exit after peer closed: %v", ctx.Err())
	}
}

func TestClientReconnectsAndPublishesFactsAfterEveryWelcome(t *testing.T) {
	var attempts atomic.Int32
	secondFacts := make(chan struct{})
	service := &controlService{connect: func(stream grpc.BidiStreamingServer[controlpb.RunnerFrame, controlpb.ServiceFrame]) error {
		attempt := attempts.Add(1)
		if err := welcome(stream); err != nil {
			return err
		}
		if _, err := receiveFacts(stream); err != nil {
			return err
		}
		if attempt == 1 {
			return status.Error(codes.Unavailable, "test disconnect")
		}
		close(secondFacts)
		<-stream.Context().Done()
		return stream.Context().Err()
	}}
	endpoint := controlServer(t, service)
	facts := &controlpb.RunnerFacts{IdleSince: timestamppb.Now()}
	client := controlClient(t, &runnerService{listSessions: func(context.Context, *runnerpb.ListSessionsRequest) (*runnerpb.ListSessionsResponse, error) {
		return &runnerpb.ListSessionsResponse{}, nil
	}}, func() (*controlpb.RunnerFacts, error) {
		return facts, nil
	}, func(context.Context, string) error { return nil }, nil)
	ctx, cancel := context.WithTimeout(context.Background(), 8*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- client.Run(ctx, Endpoint{Target: endpoint, Bearer: "token"}) }()
	select {
	case <-secondFacts:
	case <-ctx.Done():
		t.Fatalf("control stream did not reconnect: %v", ctx.Err())
	}
	cancel()
	if err := <-done; err != nil {
		t.Fatalf("control run after cancellation: %v", err)
	}
	if attempts.Load() != 2 {
		t.Fatalf("connection attempts = %d, want 2", attempts.Load())
	}
}

func TestClientStopsRetryingTerminalLocalError(t *testing.T) {
	localErr := errors.New("local journal failure")
	var attempts atomic.Int32
	service := &controlService{connect: func(stream grpc.BidiStreamingServer[controlpb.RunnerFrame, controlpb.ServiceFrame]) error {
		attempts.Add(1)
		if err := welcome(stream); err != nil {
			return err
		}
		_, err := receiveFacts(stream)
		return err
	}}
	endpoint := controlServer(t, service)
	client := controlClient(t, &runnerService{listSessions: func(context.Context, *runnerpb.ListSessionsRequest) (*runnerpb.ListSessionsResponse, error) {
		return &runnerpb.ListSessionsResponse{}, nil
	}}, func() (*controlpb.RunnerFacts, error) {
		return nil, localErr
	}, func(context.Context, string) error { return nil }, func(err error) bool { return errors.Is(err, localErr) })
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	err := client.Run(ctx, Endpoint{Target: endpoint, Bearer: "token"})
	if !errors.Is(err, localErr) {
		t.Fatalf("control run error = %v, want %v", err, localErr)
	}
	if attempts.Load() != 1 {
		t.Fatalf("connection attempts = %d, want one after terminal local error", attempts.Load())
	}
}

func TestClientCancelsAndJoinsTunneledRPC(t *testing.T) {
	rpcStarted := make(chan struct{})
	rpcCanceled := make(chan struct{})
	rpc := &runnerService{listSessions: func(ctx context.Context, _ *runnerpb.ListSessionsRequest) (*runnerpb.ListSessionsResponse, error) {
		close(rpcStarted)
		<-ctx.Done()
		close(rpcCanceled)
		return nil, status.Error(codes.Canceled, ctx.Err().Error())
	}}
	service := &controlService{connect: func(stream grpc.BidiStreamingServer[controlpb.RunnerFrame, controlpb.ServiceFrame]) error {
		if err := welcome(stream); err != nil {
			return err
		}
		if _, err := receiveFacts(stream); err != nil {
			return err
		}
		request, err := proto.Marshal(&runnerpb.ListSessionsRequest{})
		if err != nil {
			return err
		}
		if err := stream.Send(&controlpb.ServiceFrame{Frame: &controlpb.ServiceFrame_Call{Call: &tunnelpb.Call{
			CallId: 18, Method: "/agentplane.runner.Runner/ListSessions", Request: request,
		}}}); err != nil {
			return err
		}
		<-stream.Context().Done()
		return stream.Context().Err()
	}}
	endpoint := controlServer(t, service)
	client := controlClient(t, rpc, func() (*controlpb.RunnerFacts, error) { return &controlpb.RunnerFacts{}, nil }, func(context.Context, string) error { return nil }, nil)
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { _, err := client.connect(ctx, Endpoint{Target: endpoint, Bearer: "token"}); done <- err }()
	select {
	case <-rpcStarted:
	case <-time.After(5 * time.Second):
		t.Fatal("tunneled RPC did not start")
	}
	cancel()
	select {
	case err := <-done:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("control connect error after cancellation = %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("control client did not join the canceled RPC")
	}
	select {
	case <-rpcCanceled:
	default:
		t.Fatal("control client returned before the tunneled RPC observed cancellation")
	}
}

func first(values []string) string {
	if len(values) == 0 {
		return ""
	}
	return values[0]
}
