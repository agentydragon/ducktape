package streaming_test

import (
	"context"
	"errors"
	"net"
	"net/http"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/facade"
	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/wrapperspb"
)

func TestSendWithinTimesOutWithoutWaitingForSend(t *testing.T) {
	var abortCalls atomic.Int32
	ctx := streaming.WithAbortWrite(t.Context(), func() error {
		abortCalls.Add(1)
		return nil
	})
	release := make(chan struct{})
	var releaseOnce sync.Once
	releaseSend := func() { releaseOnce.Do(func() { close(release) }) }
	t.Cleanup(releaseSend)
	started := make(chan struct{})
	returned := make(chan struct{})
	err := streaming.SendWithin(ctx, 10*time.Millisecond, func(*int) error {
		close(started)
		<-release
		close(returned)
		return nil
	}, new(int))
	if !errors.Is(err, streaming.ErrSendTimeout) {
		t.Fatalf("SendWithin() = %v, want ErrSendTimeout", err)
	}
	if got := abortCalls.Load(); got != 1 {
		t.Fatalf("abort callback calls = %d, want 1 for an outstanding Send", got)
	}
	select {
	case <-started:
	default:
		t.Fatal("the underlying Send did not start")
	}
	select {
	case <-returned:
		t.Fatal("SendWithin waited for the blocked Send")
	default:
	}

	releaseSend()
	select {
	case <-returned:
	case <-time.After(5 * time.Second):
		t.Fatal("the fake Send did not return after release")
	}
}

func TestSendWithinReturnsOnContextCancellation(t *testing.T) {
	baseCtx, cancel := context.WithCancel(t.Context())
	var abortCalls atomic.Int32
	ctx := streaming.WithAbortWrite(baseCtx, func() error {
		abortCalls.Add(1)
		return nil
	})
	release := make(chan struct{})
	var releaseOnce sync.Once
	releaseSend := func() { releaseOnce.Do(func() { close(release) }) }
	t.Cleanup(releaseSend)
	started := make(chan struct{})
	returned := make(chan struct{})
	result := make(chan error, 1)
	go func() {
		result <- streaming.SendWithin(ctx, time.Minute, func(*int) error {
			close(started)
			<-release
			close(returned)
			return nil
		}, new(int))
	}()
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("the underlying Send did not start")
	}
	cancel()
	select {
	case err := <-result:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("SendWithin() = %v, want context.Canceled", err)
		}
		if got := abortCalls.Load(); got != 1 {
			t.Fatalf("abort callback calls = %d, want 1 for an outstanding Send", got)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("context cancellation did not end SendWithin")
	}
	select {
	case <-returned:
		t.Fatal("SendWithin waited for the blocked Send")
	default:
	}

	releaseSend()
	select {
	case <-returned:
	case <-time.After(5 * time.Second):
		t.Fatal("the fake Send did not return after release")
	}
}

func TestSendWithinDoesNotAbortCompletedSend(t *testing.T) {
	var abortCalls atomic.Int32
	ctx := streaming.WithAbortWrite(t.Context(), func() error {
		abortCalls.Add(1)
		return nil
	})
	wantErr := errors.New("send failed")
	for _, sendErr := range []error{nil, wantErr} {
		err := streaming.SendWithin(ctx, time.Second, func(*int) error { return sendErr }, new(int))
		if !errors.Is(err, sendErr) {
			t.Fatalf("SendWithin() = %v, want %v", err, sendErr)
		}
	}
	if got := abortCalls.Load(); got != 0 {
		t.Fatalf("abort callback calls = %d, want 0 for completed Sends", got)
	}
}

func TestRealGRPCFlowControlReleasesBlockedSendAfterDrain(t *testing.T) {
	for _, tc := range []struct {
		name         string
		transport    string
		cancelClient bool
	}{
		{name: "native drain", transport: "native"},
		{name: "facade drain", transport: "facade"},
		{name: "facade client cancellation", transport: "facade", cancelClient: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			testRealGRPCFlowControlReleasesBlockedSend(t, tc.transport, tc.cancelClient)
		})
	}
}

func testRealGRPCFlowControlReleasesBlockedSend(t *testing.T, transport string, cancelClient bool) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	draining, drain := context.WithCancel(t.Context())
	service := &flowControlService{
		draining:     draining,
		sendStarted:  make(chan int64, 16),
		sendFinished: make(chan int64, 16),
		handlerDone:  make(chan error, 1),
	}
	server := grpc.NewServer()
	server.RegisterService(&flowControlServiceDesc, service)
	if transport == "facade" {
		httpServer := &http.Server{Handler: http.NotFoundHandler()}
		facade.MountGRPC(httpServer, server)
		go func() { _ = httpServer.Serve(listener) }()
		t.Cleanup(func() { _ = httpServer.Close() })
	} else {
		go func() { _ = server.Serve(listener) }()
	}
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})

	conn, err := grpc.NewClient(
		listener.Addr().String(),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithInitialWindowSize(64<<10),
		grpc.WithInitialConnWindowSize(64<<10),
	)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	requestCtx, cancelRequest := context.WithCancel(t.Context())
	t.Cleanup(cancelRequest)
	client, err := conn.NewStream(requestCtx, &grpc.StreamDesc{ServerStreams: true}, "/agentplane.grpc.streaming.FlowControl/Watch")
	if err != nil {
		t.Fatal(err)
	}
	if err := client.SendMsg(&emptypb.Empty{}); err != nil {
		t.Fatal(err)
	}
	if err := client.CloseSend(); err != nil {
		t.Fatal(err)
	}

	deadline := time.After(5 * time.Second)
	var blockedAttempt int64
	for blockedAttempt == 0 {
		select {
		case attempt := <-service.sendStarted:
			if service.completed.Load() < attempt {
				// The payload exceeds the peer's advertised receive window. Give the transport a moment to
				// exhaust its write buffer, then verify this same SendMsg is still blocked.
				time.Sleep(50 * time.Millisecond)
				if service.completed.Load() < attempt {
					blockedAttempt = attempt
				}
			}
		case <-deadline:
			t.Fatal("no SendMsg remained blocked under the unread stream's flow-control window")
		}
	}
	if cancelClient {
		cancelRequest()
	} else {
		drain()
	}

	select {
	case err := <-service.handlerDone:
		if cancelClient {
			// Client cancellation may close the transport before the server context wins the SendWithin race.
			if !errors.Is(err, context.Canceled) && status.Code(err) != codes.Canceled && status.Code(err) != codes.Unavailable {
				t.Fatalf("handler error = %v, want cancellation or transport closure after client cancel", err)
			}
		} else if !errors.Is(err, context.Canceled) {
			t.Fatalf("handler error = %v, want context.Canceled after drain", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("draining did not return the RPC handler")
	}
	attempts := service.attempts.Load()
	if !cancelClient && attempts != blockedAttempt {
		t.Fatalf("underlying Send calls = %d, want no call after blocked write %d", attempts, blockedAttempt)
	}
	// A client cancellation can race a successful SendMsg return and start the next sequential write before the
	// server observes the canceled stream. Once the handler returns, every started SendMsg must still be released.
	for service.completed.Load() < attempts {
		select {
		case <-service.sendFinished:
		case <-time.After(5 * time.Second):
			t.Fatal("gRPC did not release every started SendMsg after the handler returned")
		}
	}
	if transport == "facade" {
		ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
		defer cancel()
		if err := conn.Invoke(ctx, "/agentplane.grpc.streaming.FlowControl/Ping", &emptypb.Empty{}, &emptypb.Empty{}); err != nil {
			t.Fatalf("unary RPC on the same client connection after aborting the stream: %v", err)
		}
	}
}

type flowControlServer interface {
	Watch(grpc.ServerStream) error
	Ping(context.Context, *emptypb.Empty) (*emptypb.Empty, error)
}

type flowControlService struct {
	draining     context.Context
	sendStarted  chan int64
	sendFinished chan int64
	handlerDone  chan error
	attempts     atomic.Int64
	completed    atomic.Int64
}

func (s *flowControlService) Watch(stream grpc.ServerStream) (err error) {
	defer func() { s.handlerDone <- err }()
	var request emptypb.Empty
	if err := stream.RecvMsg(&request); err != nil {
		return err
	}
	writeCtx, cancel := context.WithCancel(stream.Context())
	stop := context.AfterFunc(s.draining, cancel)
	defer stop()
	defer cancel()
	send := func(frame *wrapperspb.BytesValue) error {
		attempt := s.attempts.Add(1)
		select {
		case s.sendStarted <- attempt:
		default:
		}
		err := stream.SendMsg(frame)
		s.completed.Store(attempt)
		select {
		case s.sendFinished <- attempt:
		default:
		}
		return err
	}
	frame := wrapperspb.Bytes([]byte(strings.Repeat("x", 1<<20)))
	for {
		if err := streaming.SendWithin(writeCtx, 10*time.Second, send, frame); err != nil {
			return err
		}
	}
}

func (*flowControlService) Ping(context.Context, *emptypb.Empty) (*emptypb.Empty, error) {
	return &emptypb.Empty{}, nil
}

var flowControlServiceDesc = grpc.ServiceDesc{
	ServiceName: "agentplane.grpc.streaming.FlowControl",
	HandlerType: (*flowControlServer)(nil),
	Methods: []grpc.MethodDesc{{
		MethodName: "Ping",
		Handler: func(server any, ctx context.Context, decode func(any) error, _ grpc.UnaryServerInterceptor) (any, error) {
			request := new(emptypb.Empty)
			if err := decode(request); err != nil {
				return nil, err
			}
			return server.(flowControlServer).Ping(ctx, request)
		},
	}},
	Streams: []grpc.StreamDesc{{
		StreamName:    "Watch",
		Handler:       func(server any, stream grpc.ServerStream) error { return server.(flowControlServer).Watch(stream) },
		ServerStreams: true,
	}},
}

var _ flowControlServer = (*flowControlService)(nil)
