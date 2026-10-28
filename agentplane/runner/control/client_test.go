package control

import (
	"context"
	"errors"
	"net"
	"sync"
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
	return welcomeIncarnation(stream, "loopback-incarnation")
}

func welcomeIncarnation(stream grpc.BidiStreamingServer[controlpb.RunnerFrame, controlpb.ServiceFrame], uid string) error {
	frame, err := stream.Recv()
	if err != nil {
		return err
	}
	if hello := frame.GetHello(); hello == nil || hello.RunnerBootId != "test-boot-id" {
		return errors.New("first control frame was not the expected Hello")
	}
	return stream.Send(&controlpb.ServiceFrame{Frame: &controlpb.ServiceFrame_Welcome{Welcome: &controlpb.Welcome{ConnectionEpoch: 1, IncarnationUid: uid}}})
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

func TestClientKeepsStreamLiveAndFencesCallsDuringIncarnationAdoption(t *testing.T) {
	adoptionStarted := make(chan struct{})
	releaseAdoption := make(chan struct{})
	var releaseOnce sync.Once
	release := func() { releaseOnce.Do(func() { close(releaseAdoption) }) }
	t.Cleanup(release)
	framesToRunner := make(chan *controlpb.ServiceFrame, 32)
	livenessHeartbeats := make(chan *controlpb.RunnerHeartbeat, 16)
	factHeartbeats := make(chan *controlpb.RunnerHeartbeat, 2)
	replies := make(chan *tunnelpb.Reply, 4)
	service := &controlService{connect: func(stream grpc.BidiStreamingServer[controlpb.RunnerFrame, controlpb.ServiceFrame]) error {
		if err := welcome(stream); err != nil {
			return err
		}
		go func() {
			for {
				select {
				case frame := <-framesToRunner:
					if err := stream.Send(frame); err != nil {
						return
					}
				case <-stream.Context().Done():
					return
				}
			}
		}()
		for {
			frame, err := stream.Recv()
			if err != nil {
				return err
			}
			switch {
			case frame.GetHeartbeat() != nil && frame.GetHeartbeat().GetFacts() == nil:
				select {
				case livenessHeartbeats <- frame.GetHeartbeat():
				case <-stream.Context().Done():
					return stream.Context().Err()
				}
				select {
				case framesToRunner <- &controlpb.ServiceFrame{Frame: &controlpb.ServiceFrame_Heartbeat{Heartbeat: &controlpb.Heartbeat{}}}:
				case <-stream.Context().Done():
					return stream.Context().Err()
				}
			case frame.GetHeartbeat() != nil:
				select {
				case factHeartbeats <- frame.GetHeartbeat():
				case <-stream.Context().Done():
					return stream.Context().Err()
				}
			case frame.GetReply() != nil:
				select {
				case replies <- frame.GetReply():
				case <-stream.Context().Done():
					return stream.Context().Err()
				}
			default:
				return errors.New("unexpected Runner control frame")
			}
		}
	}}
	endpoint := controlServer(t, service)
	var factsCalls, relayedCalls atomic.Int32
	factsSnapshot := &controlpb.RunnerFacts{IdleSince: timestamppb.Now()}
	rpc := &runnerService{listSessions: func(context.Context, *runnerpb.ListSessionsRequest) (*runnerpb.ListSessionsResponse, error) {
		relayedCalls.Add(1)
		return &runnerpb.ListSessionsResponse{}, nil
	}}
	client := controlClient(t, rpc, func() (*controlpb.RunnerFacts, error) {
		factsCalls.Add(1)
		return factsSnapshot, nil
	}, func(ctx context.Context, uid string) error {
		if uid != "loopback-incarnation" {
			return errors.New("unexpected Sandbox incarnation")
		}
		close(adoptionStarted)
		select {
		case <-releaseAdoption:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}, nil)
	client.heartbeatInterval = 10 * time.Millisecond
	client.streamLiveness = 60 * time.Millisecond
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	connected := make(chan error, 1)
	go func() {
		_, err := client.connect(ctx, Endpoint{Target: endpoint, Bearer: "token"})
		connected <- err
	}()
	select {
	case <-adoptionStarted:
	case <-ctx.Done():
		t.Fatalf("incarnation adoption did not start: %v", ctx.Err())
	}
	request, err := proto.Marshal(&runnerpb.ListSessionsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	select {
	case framesToRunner <- &controlpb.ServiceFrame{Frame: &controlpb.ServiceFrame_Call{Call: &tunnelpb.Call{CallId: 1, Method: "/agentplane.runner.Runner/ListSessions", Request: request}}}:
	case <-ctx.Done():
		t.Fatalf("could not send pre-adoption call: %v", ctx.Err())
	}
	select {
	case reply := <-replies:
		if reply.CallId != 1 || reply.Code != int32(codes.Unavailable) {
			t.Fatalf("pre-adoption call reply = %v, want Unavailable", reply)
		}
	case <-ctx.Done():
		t.Fatalf("pre-adoption call was not refused: %v", ctx.Err())
	}
	for range 8 {
		select {
		case heartbeat := <-livenessHeartbeats:
			if heartbeat.Facts != nil {
				t.Fatalf("pre-adoption liveness heartbeat published old facts: %v", heartbeat.Facts)
			}
		case err := <-connected:
			t.Fatalf("control stream ended during blocked adoption: %v", err)
		case <-ctx.Done():
			t.Fatalf("control stream did not remain live through several heartbeats during adoption: %v", ctx.Err())
		}
	}
	if got := factsCalls.Load(); got != 0 {
		t.Fatalf("Facts calls before adoption = %d, want 0", got)
	}
	if got := relayedCalls.Load(); got != 0 {
		t.Fatalf("Runner methods before adoption = %d, want 0", got)
	}

	release()
	select {
	case heartbeat := <-factHeartbeats:
		if heartbeat.Facts == nil {
			t.Fatal("post-adoption heartbeat omitted Runner facts")
		}
	case <-ctx.Done():
		t.Fatalf("Runner facts were not published after adoption: %v", ctx.Err())
	}
	select {
	case framesToRunner <- &controlpb.ServiceFrame{Frame: &controlpb.ServiceFrame_Call{Call: &tunnelpb.Call{CallId: 2, Method: "/agentplane.runner.Runner/ListSessions", Request: request}}}:
	case <-ctx.Done():
		t.Fatalf("could not send post-adoption call: %v", ctx.Err())
	}
	select {
	case reply := <-replies:
		if reply.CallId != 2 || reply.Code != int32(codes.OK) {
			t.Fatalf("post-adoption call reply = %v, want OK", reply)
		}
	case <-ctx.Done():
		t.Fatalf("post-adoption call was not relayed: %v", ctx.Err())
	}
	if got := relayedCalls.Load(); got != 1 {
		t.Fatalf("Runner methods after adoption = %d, want 1", got)
	}
	cancel()
	if err := <-connected; !errors.Is(err, context.Canceled) {
		t.Fatalf("control client after cancellation = %v, want context.Canceled", err)
	}
}

func TestClientReturnsIncarnationAdoptionFailure(t *testing.T) {
	adoptionStarted := make(chan struct{})
	failAdoption := make(chan struct{})
	adoptionErr := errors.New("incarnation adoption failed")
	livenessHeartbeats := make(chan *controlpb.RunnerHeartbeat, 2)
	service := &controlService{connect: func(stream grpc.BidiStreamingServer[controlpb.RunnerFrame, controlpb.ServiceFrame]) error {
		if err := welcome(stream); err != nil {
			return err
		}
		for {
			frame, err := stream.Recv()
			if err != nil {
				return err
			}
			if heartbeat := frame.GetHeartbeat(); heartbeat != nil {
				select {
				case livenessHeartbeats <- heartbeat:
				case <-stream.Context().Done():
					return stream.Context().Err()
				}
				if err := stream.Send(&controlpb.ServiceFrame{Frame: &controlpb.ServiceFrame_Heartbeat{Heartbeat: &controlpb.Heartbeat{}}}); err != nil {
					return err
				}
			}
		}
	}}
	endpoint := controlServer(t, service)
	var factsCalls atomic.Int32
	client := controlClient(t, &runnerService{listSessions: func(context.Context, *runnerpb.ListSessionsRequest) (*runnerpb.ListSessionsResponse, error) {
		return &runnerpb.ListSessionsResponse{}, nil
	}}, func() (*controlpb.RunnerFacts, error) {
		factsCalls.Add(1)
		return &controlpb.RunnerFacts{}, nil
	}, func(ctx context.Context, _ string) error {
		close(adoptionStarted)
		select {
		case <-failAdoption:
			return adoptionErr
		case <-ctx.Done():
			return ctx.Err()
		}
	}, nil)
	client.heartbeatInterval = 10 * time.Millisecond
	client.streamLiveness = 2 * time.Second
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	connected := make(chan error, 1)
	go func() {
		_, err := client.connect(ctx, Endpoint{Target: endpoint, Bearer: "token"})
		connected <- err
	}()
	select {
	case <-adoptionStarted:
	case <-ctx.Done():
		t.Fatalf("incarnation adoption did not start: %v", ctx.Err())
	}
	select {
	case heartbeat := <-livenessHeartbeats:
		if heartbeat.Facts != nil {
			t.Fatalf("pre-adoption heartbeat published facts: %v", heartbeat.Facts)
		}
	case <-ctx.Done():
		t.Fatalf("control stream did not remain live during adoption: %v", ctx.Err())
	}
	close(failAdoption)
	select {
	case err := <-connected:
		if !errors.Is(err, adoptionErr) {
			t.Fatalf("control client error = %v, want adoption failure", err)
		}
	case <-ctx.Done():
		t.Fatalf("control client did not return adoption failure: %v", ctx.Err())
	}
	if got := factsCalls.Load(); got != 0 {
		t.Fatalf("Facts calls after failed adoption = %d, want 0", got)
	}
}

func TestClientJoinsCanceledAdoptionBeforeReconnect(t *testing.T) {
	dropFirstStream := make(chan struct{})
	releaseFirstAdoption := make(chan struct{})
	var releaseOnce sync.Once
	release := func() { releaseOnce.Do(func() { close(releaseFirstAdoption) }) }
	t.Cleanup(release)
	var attempts atomic.Int32
	service := &controlService{connect: func(stream grpc.BidiStreamingServer[controlpb.RunnerFrame, controlpb.ServiceFrame]) error {
		attempt := attempts.Add(1)
		uid := "old-incarnation"
		if attempt == 2 {
			uid = "new-incarnation"
		}
		if err := welcomeIncarnation(stream, uid); err != nil {
			return err
		}
		if attempt == 1 {
			select {
			case <-dropFirstStream:
				return status.Error(codes.Unavailable, "peer disconnected during adoption")
			case <-stream.Context().Done():
				return stream.Context().Err()
			}
		}
		<-stream.Context().Done()
		return stream.Context().Err()
	}}
	endpoint := controlServer(t, service)
	var activeAdoptions, maxActiveAdoptions atomic.Int32
	oldAdoptionStarted := make(chan struct{})
	oldAdoptionCanceled := make(chan error, 1)
	newAdoptionStarted := make(chan struct{})
	client := controlClient(t, &runnerService{listSessions: func(context.Context, *runnerpb.ListSessionsRequest) (*runnerpb.ListSessionsResponse, error) {
		return &runnerpb.ListSessionsResponse{}, nil
	}}, func() (*controlpb.RunnerFacts, error) { return &controlpb.RunnerFacts{}, nil }, func(ctx context.Context, uid string) error {
		active := activeAdoptions.Add(1)
		for max := maxActiveAdoptions.Load(); active > max; max = maxActiveAdoptions.Load() {
			if maxActiveAdoptions.CompareAndSwap(max, active) {
				break
			}
		}
		defer activeAdoptions.Add(-1)
		switch uid {
		case "old-incarnation":
			close(oldAdoptionStarted)
			<-ctx.Done()
			oldAdoptionCanceled <- ctx.Err()
			<-releaseFirstAdoption
			return ctx.Err()
		case "new-incarnation":
			close(newAdoptionStarted)
			return nil
		default:
			return errors.New("unexpected Sandbox incarnation")
		}
	}, nil)
	client.heartbeatInterval = 10 * time.Millisecond
	client.streamLiveness = time.Second
	firstCtx, cancelFirst := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancelFirst)
	type connectResult struct {
		err                   error
		activeAdoptionsOnExit int32
	}
	firstConnected := make(chan connectResult, 1)
	go func() {
		_, err := client.connect(firstCtx, Endpoint{Target: endpoint, Bearer: "token"})
		firstConnected <- connectResult{err: err, activeAdoptionsOnExit: activeAdoptions.Load()}
	}()
	select {
	case <-oldAdoptionStarted:
	case <-firstCtx.Done():
		t.Fatalf("old-incarnation adoption did not start: %v", firstCtx.Err())
	}
	close(dropFirstStream)
	select {
	case err := <-oldAdoptionCanceled:
		if !errors.Is(err, context.Canceled) || firstCtx.Err() != nil {
			t.Fatalf("peer disconnect cancellation = %v, parent context = %v", err, firstCtx.Err())
		}
	case <-firstCtx.Done():
		t.Fatalf("peer disconnect did not cancel the attempt adoption: %v", firstCtx.Err())
	}
	if active := activeAdoptions.Load(); active != 1 {
		t.Fatalf("active adoptions before cleanup = %d, want the old adoption still owned", active)
	}
	release()
	select {
	case result := <-firstConnected:
		if status.Code(result.err) != codes.Unavailable {
			t.Fatalf("first control attempt error = %v, want Unavailable", result.err)
		}
		if result.activeAdoptionsOnExit != 0 {
			t.Fatalf("first control attempt returned with %d active adoptions", result.activeAdoptionsOnExit)
		}
	case <-firstCtx.Done():
		t.Fatalf("first control attempt did not join adoption cleanup: %v", firstCtx.Err())
	}
	cancelFirst()
	if active := activeAdoptions.Load(); active != 0 {
		t.Fatalf("adoption still active when the first attempt returned: %d", active)
	}

	secondCtx, cancelSecond := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancelSecond()
	secondConnected := make(chan error, 1)
	go func() {
		_, err := client.connect(secondCtx, Endpoint{Target: endpoint, Bearer: "token"})
		secondConnected <- err
	}()
	select {
	case <-newAdoptionStarted:
	case <-secondCtx.Done():
		t.Fatalf("replacement incarnation adoption did not start: %v", secondCtx.Err())
	}
	cancelSecond()
	select {
	case err := <-secondConnected:
		if err == nil {
			t.Fatal("second control attempt returned without a stream error after cancellation")
		}
	case <-time.After(5 * time.Second):
		t.Fatal("second control attempt did not stop after cancellation")
	}
	if attempts.Load() != 2 || maxActiveAdoptions.Load() != 1 || activeAdoptions.Load() != 0 {
		t.Fatalf("reconnect/adoption lifecycle = attempts:%d max_active:%d active:%d", attempts.Load(), maxActiveAdoptions.Load(), activeAdoptions.Load())
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
