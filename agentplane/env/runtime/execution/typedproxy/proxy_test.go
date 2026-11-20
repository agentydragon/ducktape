package typedproxy

import (
	"context"
	"fmt"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	pb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/native"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/typedbackend"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
)

func connect(t *testing.T, s pb.ExecutorServer) pb.ExecutorClient {
	t.Helper()
	l := bufconn.Listen(1 << 20)
	g := grpc.NewServer()
	pb.RegisterExecutorServer(g, s)
	go func() { _ = g.Serve(l) }()
	t.Cleanup(func() { g.Stop(); _ = l.Close() })
	conn, err := grpc.NewClient("passthrough:///test", grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return l.Dial() }), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return pb.NewExecutorClient(conn)
}

func TestProxyRetainsScopedSessionAndRunsProcess(t *testing.T) {
	core := native.New()
	t.Cleanup(func() { _ = core.Close() })
	backend := connect(t, typedbackend.New(core))
	p, err := New(context.Background(), backend, func(ctx context.Context, scope *pb.ExecutionScope, _ bool, fn func(context.Context) error) error {
		if scope.GetSessionId() == "" {
			return status.Error(codes.PermissionDenied, "missing scope")
		}
		return fn(ctx)
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = p.Close() })
	client := connect(t, p)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	scope := &pb.ExecutionScope{SessionId: "one"}
	stream, err := client.OpenSession(ctx, &pb.OpenSessionRequest{Scope: scope, RequestId: "open"})
	if err != nil {
		t.Fatal(err)
	}
	first, err := stream.Recv()
	if err != nil || first.GetStatus().GetOutcome() != pb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("open = %v, %v", first, err)
	}
	id := first.GetExecutionSessionId()
	if !p.Active() {
		t.Fatal("open session not active")
	}
	_, err = client.StartProcess(context.Background(), &pb.StartProcessRequest{Scope: &pb.ExecutionScope{SessionId: "other"}, RequestId: "wrong", ExecutionSessionId: id, ProcessId: "p", Argv: []string{"/bin/echo", "hello"}, Cwd: "file:///tmp"})
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("wrong scope: %v", err)
	}
	started, err := client.StartProcess(context.Background(), &pb.StartProcessRequest{Scope: scope, RequestId: "start", ExecutionSessionId: id, ProcessId: "p", Argv: []string{"/bin/echo", "hello"}, Cwd: "file:///tmp"})
	if err != nil || started.GetStatus().GetOutcome() != pb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("start = %v, %v", started, err)
	}
	out, err := client.ReadProcess(context.Background(), &pb.ReadProcessRequest{Scope: scope, RequestId: "read", ExecutionSessionId: id, ProcessId: "p", MaxBytes: 1024, WaitMs: 1000})
	if err != nil || out.GetStatus().GetOutcome() != pb.ExecutionStatus_OUTCOME_COMPLETED || len(out.Chunks) == 0 {
		t.Fatalf("read = %v, %v", out, err)
	}
	drainCtx, stop := context.WithTimeout(context.Background(), 5*time.Second)
	defer stop()
	if err := p.Drain(drainCtx); err != nil {
		t.Fatal(err)
	}
	if p.Active() {
		t.Fatal("session remained active after drain")
	}
}

type delayedBackend struct {
	pb.UnimplementedExecutorServer
	entered  chan struct{}
	release  chan struct{}
	finished chan error
}

func (b *delayedBackend) OpenSession(_ *pb.OpenSessionRequest, stream pb.Executor_OpenSessionServer) error {
	if err := stream.Send(&pb.OpenSessionResponse{Status: &pb.ExecutionStatus{Outcome: pb.ExecutionStatus_OUTCOME_COMPLETED}, ExecutionSessionId: "backend-session"}); err != nil {
		return err
	}
	<-stream.Context().Done()
	return nil
}

func (b *delayedBackend) StartProcess(ctx context.Context, _ *pb.StartProcessRequest) (*pb.StartProcessResponse, error) {
	close(b.entered)
	select {
	case <-b.release:
		b.finished <- nil
		return &pb.StartProcessResponse{Status: &pb.ExecutionStatus{Outcome: pb.ExecutionStatus_OUTCOME_COMPLETED}}, nil
	case <-ctx.Done():
		b.finished <- ctx.Err()
		return nil, ctx.Err()
	}
}

func TestCanceledCallerDoesNotCancelAdmittedMutation(t *testing.T) {
	b := &delayedBackend{entered: make(chan struct{}), release: make(chan struct{}), finished: make(chan error, 1)}
	backend := connect(t, b)
	p, err := New(context.Background(), backend, func(ctx context.Context, _ *pb.ExecutionScope, _ bool, fn func(context.Context) error) error {
		return fn(ctx)
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = p.Close() })
	client := connect(t, p)
	streamCtx, stopStream := context.WithCancel(context.Background())
	defer stopStream()
	scope := &pb.ExecutionScope{SessionId: "one"}
	stream, err := client.OpenSession(streamCtx, &pb.OpenSessionRequest{Scope: scope, RequestId: "open"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = stream.Recv(); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() {
		_, err := client.StartProcess(ctx, &pb.StartProcessRequest{Scope: scope, RequestId: "start", ExecutionSessionId: "backend-session"})
		done <- err
	}()
	<-b.entered
	cancel()
	if err := <-done; status.Code(err) != codes.Canceled {
		t.Fatalf("caller cancellation = %v", err)
	}
	close(b.release)
	select {
	case err := <-b.finished:
		if err != nil {
			t.Fatalf("backend was canceled: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("backend mutation did not finish")
	}
}

type blockedOpenBackend struct {
	pb.UnimplementedExecutorServer
	entered chan struct{}
}

func (b *blockedOpenBackend) OpenSession(_ *pb.OpenSessionRequest, stream pb.Executor_OpenSessionServer) error {
	close(b.entered)
	<-stream.Context().Done()
	return nil
}

func TestDrainCancelsOpeningSession(t *testing.T) {
	b := &blockedOpenBackend{entered: make(chan struct{})}
	p, err := New(context.Background(), connect(t, b), func(ctx context.Context, _ *pb.ExecutionScope, _ bool, fn func(context.Context) error) error {
		return fn(ctx)
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = p.Close() })
	c := connect(t, p)
	stream, err := c.OpenSession(context.Background(), &pb.OpenSessionRequest{Scope: &pb.ExecutionScope{SessionId: "one"}, RequestId: "open"})
	if err != nil {
		t.Fatal(err)
	}
	<-b.entered
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err = p.Drain(ctx); err != nil {
		t.Fatalf("drain opening session: %v", err)
	}
	if p.Active() {
		t.Fatal("opening session remains active")
	}
	if _, err = stream.Recv(); err == nil {
		t.Fatal("expected interrupted opening stream")
	}
}

func TestResumeClosesPreviousConnectionFiles(t *testing.T) {
	core := native.New()
	t.Cleanup(func() { _ = core.Close() })
	p, err := New(context.Background(), connect(t, typedbackend.New(core)), func(ctx context.Context, _ *pb.ExecutionScope, _ bool, fn func(context.Context) error) error {
		return fn(ctx)
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = p.Close() })
	client := connect(t, p)
	scope := &pb.ExecutionScope{SessionId: "one"}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	old, err := client.OpenSession(ctx, &pb.OpenSessionRequest{Scope: scope, RequestId: "open"})
	if err != nil {
		t.Fatal(err)
	}
	first, err := old.Recv()
	if err != nil {
		t.Fatal(err)
	}
	id := first.GetExecutionSessionId()
	path := filepath.Join(t.TempDir(), "sample")
	if err := os.WriteFile(path, []byte("hello"), 0600); err != nil {
		t.Fatal(err)
	}
	opened, err := client.OpenFile(ctx, &pb.OpenFileRequest{Scope: scope, ExecutionSessionId: id, RequestId: "file", HandleId: "handle", Path: (&url.URL{Scheme: "file", Path: path}).String()})
	if err != nil || opened.GetStatus().GetOutcome() != pb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("open file: %v %v", opened, err)
	}
	newer, err := client.OpenSession(ctx, &pb.OpenSessionRequest{Scope: scope, RequestId: "resume", ResumeSessionId: &id})
	if err != nil {
		t.Fatal(err)
	}
	if response, err := newer.Recv(); err != nil || response.GetStatus().GetOutcome() != pb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("resume: %v %v", response, err)
	}
	response, err := client.ReadFile(ctx, &pb.ReadFileRequest{Scope: scope, ExecutionSessionId: id, RequestId: "read", HandleId: "handle", Length: 10})
	if err != nil || response.GetStatus().GetOutcome() != pb.ExecutionStatus_OUTCOME_REJECTED {
		t.Fatalf("old connection handle still open: %v %v", response, err)
	}
}

type delayedCloseBackend struct {
	pb.UnimplementedExecutorServer
	entered, release chan struct{}
}

func (b *delayedCloseBackend) OpenSession(_ *pb.OpenSessionRequest, stream pb.Executor_OpenSessionServer) error {
	if err := stream.Send(&pb.OpenSessionResponse{Status: &pb.ExecutionStatus{Outcome: pb.ExecutionStatus_OUTCOME_COMPLETED}, ExecutionSessionId: "retained"}); err != nil {
		return err
	}
	<-stream.Context().Done()
	return nil
}

func (b *delayedCloseBackend) OpenFile(context.Context, *pb.OpenFileRequest) (*pb.OpenFileResponse, error) {
	return &pb.OpenFileResponse{Status: &pb.ExecutionStatus{Outcome: pb.ExecutionStatus_OUTCOME_COMPLETED}}, nil
}

func (b *delayedCloseBackend) CloseFile(ctx context.Context, _ *pb.CloseFileRequest) (*pb.CloseFileResponse, error) {
	close(b.entered)
	select {
	case <-b.release:
		return &pb.CloseFileResponse{Status: &pb.ExecutionStatus{Outcome: pb.ExecutionStatus_OUTCOME_COMPLETED}}, nil
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

func TestResumeWaitsForPreviousHandleCleanup(t *testing.T) {
	b := &delayedCloseBackend{entered: make(chan struct{}), release: make(chan struct{})}
	p, err := New(context.Background(), connect(t, b), func(ctx context.Context, _ *pb.ExecutionScope, _ bool, fn func(context.Context) error) error {
		return fn(ctx)
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = p.Close() })
	c := connect(t, p)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	scope := &pb.ExecutionScope{SessionId: "one"}
	old, err := c.OpenSession(ctx, &pb.OpenSessionRequest{Scope: scope, RequestId: "open"})
	if err != nil {
		t.Fatal(err)
	}
	first, err := old.Recv()
	if err != nil {
		t.Fatal(err)
	}
	id := first.GetExecutionSessionId()
	if _, err = c.OpenFile(ctx, &pb.OpenFileRequest{Scope: scope, ExecutionSessionId: id, RequestId: "file", HandleId: "h", Path: "file:///tmp/file"}); err != nil {
		t.Fatal(err)
	}
	resumed, err := c.OpenSession(ctx, &pb.OpenSessionRequest{Scope: scope, RequestId: "resume", ResumeSessionId: &id})
	if err != nil {
		t.Fatal(err)
	}
	<-b.entered
	received := make(chan error, 1)
	go func() { _, err := resumed.Recv(); received <- err }()
	select {
	case err := <-received:
		t.Fatalf("resumed before cleanup completed: %v", err)
	case <-time.After(100 * time.Millisecond):
	}
	close(b.release)
	select {
	case err := <-received:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("resume did not finish after cleanup")
	}
}

func TestProcessChangeForwardedOnHeldSession(t *testing.T) {
	core := native.New()
	t.Cleanup(func() { _ = core.Close() })
	p, err := New(context.Background(), connect(t, typedbackend.New(core)), func(ctx context.Context, _ *pb.ExecutionScope, _ bool, fn func(context.Context) error) error {
		return fn(ctx)
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = p.Close() })
	c := connect(t, p)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	scope := &pb.ExecutionScope{SessionId: "one"}
	stream, err := c.OpenSession(ctx, &pb.OpenSessionRequest{Scope: scope, RequestId: "open"})
	if err != nil {
		t.Fatal(err)
	}
	first, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	res, err := c.StartProcess(ctx, &pb.StartProcessRequest{Scope: scope, ExecutionSessionId: first.ExecutionSessionId, RequestId: "start", ProcessId: "change", Argv: []string{"/bin/sh", "-c", "echo output"}, Cwd: "file:///tmp"})
	if err != nil || res.GetStatus().GetOutcome() != pb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("start %v %v", res, err)
	}
	change, err := stream.Recv()
	if err != nil || !change.GetProcessChanged() || len(change.GetNativeFrames()) != 0 {
		t.Fatalf("change %v %v", change, err)
	}
}

type blockingStream struct {
	ctx              context.Context
	started, release chan struct{}
	once             sync.Once
}

func (s *blockingStream) Send(*pb.OpenSessionResponse) error {
	s.once.Do(func() { close(s.started) })
	<-s.release
	return nil
}
func (s *blockingStream) SetHeader(metadata.MD) error  { return nil }
func (s *blockingStream) SendHeader(metadata.MD) error { return nil }
func (s *blockingStream) SetTrailer(metadata.MD)       {}
func (s *blockingStream) Context() context.Context     { return s.ctx }
func (s *blockingStream) SendMsg(any) error            { return nil }
func (s *blockingStream) RecvMsg(any) error            { return nil }
func TestDrainDoesNotWaitForBlockedResponseSend(t *testing.T) {
	core := native.New()
	t.Cleanup(func() { _ = core.Close() })
	p, err := New(context.Background(), connect(t, typedbackend.New(core)), func(ctx context.Context, _ *pb.ExecutionScope, _ bool, fn func(context.Context) error) error {
		return fn(ctx)
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = p.Close() })
	stream := &blockingStream{ctx: t.Context(), started: make(chan struct{}), release: make(chan struct{})}
	done := make(chan error, 1)
	go func() {
		done <- p.OpenSession(&pb.OpenSessionRequest{Scope: &pb.ExecutionScope{SessionId: "one"}, RequestId: "open"}, stream)
	}()
	select {
	case <-stream.started:
	case <-time.After(5 * time.Second):
		close(stream.release)
		t.Fatal("response send did not start")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	err = p.Drain(ctx)
	close(stream.release)
	if err != nil {
		t.Fatalf("drain blocked: %v", err)
	}
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("open did not return")
	}
}

func TestClosedHandlesDoNotAccumulate(t *testing.T) {
	core := native.New()
	t.Cleanup(func() { _ = core.Close() })
	p, err := New(context.Background(), connect(t, typedbackend.New(core)), func(ctx context.Context, _ *pb.ExecutionScope, _ bool, fn func(context.Context) error) error {
		return fn(ctx)
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = p.Close() })
	c := connect(t, p)
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	scope := &pb.ExecutionScope{SessionId: "one"}
	stream, err := c.OpenSession(ctx, &pb.OpenSessionRequest{Scope: scope, RequestId: "open"})
	if err != nil {
		t.Fatal(err)
	}
	first, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	id := first.ExecutionSessionId
	path := filepath.Join(t.TempDir(), "file")
	if err = os.WriteFile(path, []byte("data"), 0600); err != nil {
		t.Fatal(err)
	}
	for i := range 16 {
		handle := fmt.Sprintf("handle-%d", i)
		opened, err := c.OpenFile(ctx, &pb.OpenFileRequest{Scope: scope, RequestId: "open-file", ExecutionSessionId: id, HandleId: handle, Path: (&url.URL{Scheme: "file", Path: path}).String()})
		if err != nil || opened.GetStatus().GetOutcome() != pb.ExecutionStatus_OUTCOME_COMPLETED {
			t.Fatalf("open: %v %v", opened, err)
		}
		closed, err := c.CloseFile(ctx, &pb.CloseFileRequest{Scope: scope, RequestId: "close-file", ExecutionSessionId: id, HandleId: handle})
		if err != nil || closed.GetStatus().GetOutcome() != pb.ExecutionStatus_OUTCOME_COMPLETED {
			t.Fatalf("close: %v %v", closed, err)
		}
	}
	p.mu.Lock()
	count := len(p.sessions[id].handles)
	p.mu.Unlock()
	if count != 0 {
		t.Fatalf("%d closed handles retained", count)
	}
}
