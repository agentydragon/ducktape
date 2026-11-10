package streaming_test

import (
	"context"
	"errors"
	"net"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/streaming"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/wrapperspb"
)

func TestSendWithinTimesOutWithoutWaitingForSend(t *testing.T) {
	ctx := t.Context()
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
	ctx, cancel := context.WithCancel(t.Context())
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

func TestSendWithinReturnsTheCompletedSendsResult(t *testing.T) {
	ctx := t.Context()
	wantErr := errors.New("send failed")
	for _, sendErr := range []error{nil, wantErr} {
		err := streaming.SendWithin(ctx, time.Second, func(*int) error { return sendErr }, new(int))
		if !errors.Is(err, sendErr) {
			t.Fatalf("SendWithin() = %v, want %v", err, sendErr)
		}
	}
}

func TestRealGRPCFlowControlReleasesBlockedSend(t *testing.T) {
	for _, tc := range []struct {
		name          string
		cancelClient  bool
		writeTimeout  time.Duration
		expectTimeout bool
	}{
		{name: "drain"},
		{name: "client cancellation", cancelClient: true},
		{name: "write timeout", writeTimeout: 250 * time.Millisecond, expectTimeout: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			testRealGRPCFlowControlReleasesBlockedSend(t, tc.cancelClient, tc.writeTimeout, tc.expectTimeout)
		})
	}
}

func testRealGRPCFlowControlReleasesBlockedSend(t *testing.T, cancelClient bool, writeTimeout time.Duration, expectTimeout bool) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	draining, drain := context.WithCancel(t.Context())
	if writeTimeout <= 0 {
		writeTimeout = 10 * time.Second
	}
	service := &flowControlService{
		draining:     draining,
		writeTimeout: writeTimeout,
		sendChange:   make(chan struct{}, 1),
		handlerDone:  make(chan error, 1),
	}
	server := grpc.NewServer()
	server.RegisterService(&flowControlServiceDesc, service)
	go func() { _ = server.Serve(listener) }()
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

	deadline := time.NewTimer(5 * time.Second)
	defer deadline.Stop()
	var blockedAttempt int64
	for blockedAttempt == 0 {
		attempt := service.attempts.Load()
		if service.completed.Load() < attempt {
			// Wait for completion or a short observation window. The payload exceeds the peer's advertised
			// receive window, so an incomplete send after the window shows that this SendMsg is blocked.
			observation := time.NewTimer(50 * time.Millisecond)
			select {
			case <-service.sendChange:
				observation.Stop()
			case <-observation.C:
				attempt = service.attempts.Load()
				if service.completed.Load() < attempt {
					blockedAttempt = attempt
				}
			case <-deadline.C:
				observation.Stop()
				t.Fatal("no SendMsg remained blocked under the unread stream's flow-control window")
			}
		} else {
			select {
			case <-service.sendChange:
			case <-deadline.C:
				t.Fatal("no SendMsg remained blocked under the unread stream's flow-control window")
			}
		}
	}
	if cancelClient {
		cancelRequest()
	} else if !expectTimeout {
		drain()
	}

	select {
	case err := <-service.handlerDone:
		if expectTimeout {
			if status.Code(err) != codes.DeadlineExceeded {
				t.Fatalf("handler error = %v, want DeadlineExceeded after the bounded write", err)
			}
		} else if cancelClient {
			// Client cancellation may close the transport before the server context wins the SendWithin race.
			if !errors.Is(err, context.Canceled) && status.Code(err) != codes.Canceled && status.Code(err) != codes.Unavailable {
				t.Fatalf("handler error = %v, want cancellation or transport closure after client cancel", err)
			}
		} else if !errors.Is(err, context.Canceled) {
			t.Fatalf("handler error = %v, want context.Canceled after drain", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("ending the blocked write did not return the RPC handler")
	}
	attempts := service.attempts.Load()
	if service.overlappingWrites.Load() {
		t.Fatal("started another SendMsg while the previous write was still outstanding")
	}
	// Peer cancellation and the drain's AfterFunc can race a successful SendMsg return. A subsequent sequential
	// write is allowed until cancellation is observed; every started write must finish after the handler exits.
	doneDeadline := time.NewTimer(5 * time.Second)
	defer doneDeadline.Stop()
	for service.completed.Load() < attempts {
		select {
		case <-service.sendChange:
		case <-doneDeadline.C:
			t.Fatal("gRPC did not release every started SendMsg after the handler returned")
		}
	}
}

type flowControlServer interface {
	Watch(grpc.ServerStream) error
}

type flowControlService struct {
	draining          context.Context
	writeTimeout      time.Duration
	sendChange        chan struct{}
	handlerDone       chan error
	attempts          atomic.Int64
	completed         atomic.Int64
	overlappingWrites atomic.Bool
}

func (s *flowControlService) notifySendChange() {
	select {
	case s.sendChange <- struct{}{}:
	default:
	}
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
		if s.completed.Load() != attempt-1 {
			s.overlappingWrites.Store(true)
		}
		s.notifySendChange()
		err := stream.SendMsg(frame)
		s.completed.Add(1)
		s.notifySendChange()
		return err
	}
	frame := wrapperspb.Bytes([]byte(strings.Repeat("x", 1<<20)))
	for {
		if err := streaming.SendWithin(writeCtx, s.writeTimeout, send, frame); err != nil {
			if errors.Is(err, streaming.ErrSendTimeout) {
				return status.Error(codes.DeadlineExceeded, "flow-control write deadline expired")
			}
			return err
		}
	}
}

var flowControlServiceDesc = grpc.ServiceDesc{
	ServiceName: "agentplane.grpc.streaming.FlowControl",
	HandlerType: (*flowControlServer)(nil),
	Streams: []grpc.StreamDesc{{
		StreamName:    "Watch",
		Handler:       func(server any, stream grpc.ServerStream) error { return server.(flowControlServer).Watch(stream) },
		ServerStreams: true,
	}},
}

var _ flowControlServer = (*flowControlService)(nil)
