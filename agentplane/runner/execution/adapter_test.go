package execution

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	executionpb "github.com/agentydragon/ducktape/agentplane/proto/executionpb"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	runnerexecpb "github.com/agentydragon/ducktape/agentplane/runner/runner_execpb"
	"github.com/coder/websocket"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
)

type nativeRequest struct {
	ID     string          `json:"id"`
	Method string          `json:"method"`
	Params json.RawMessage `json:"params"`
}
type peer struct {
	mu          sync.Mutex
	requests    []nativeRequest
	payloads    [][]byte
	acquireErr  error
	acquireGate <-chan struct{}
	acquireWait chan<- struct{}
	initialized chan struct{}
	nextSession atomic.Int32
	handle      func(context.Context, *websocket.Conn, nativeRequest) bool
	sessionID   func(nativeRequest) string
}

type blockingOpenSessionStream struct {
	ctx     context.Context
	started chan struct{}
	release chan struct{}
	once    sync.Once
}

func (s *blockingOpenSessionStream) Send(*executionpb.OpenSessionResponse) error {
	s.once.Do(func() { close(s.started) })
	<-s.release
	return nil
}
func (s *blockingOpenSessionStream) SetHeader(metadata.MD) error  { return nil }
func (s *blockingOpenSessionStream) SendHeader(metadata.MD) error { return nil }
func (s *blockingOpenSessionStream) SetTrailer(metadata.MD)       {}
func (s *blockingOpenSessionStream) Context() context.Context     { return s.ctx }
func (s *blockingOpenSessionStream) SendMsg(any) error            { return nil }
func (s *blockingOpenSessionStream) RecvMsg(any) error            { return nil }

func (p *peer) serve(w http.ResponseWriter, r *http.Request) {
	ws, err := websocket.Accept(w, r, nil)
	if err != nil {
		return
	}
	defer ws.CloseNow()
	for {
		_, payload, err := ws.Read(r.Context())
		if err != nil {
			return
		}
		var request nativeRequest
		if json.Unmarshal(payload, &request) != nil {
			return
		}
		p.mu.Lock()
		p.requests = append(p.requests, request)
		p.payloads = append(p.payloads, payload)
		p.mu.Unlock()
		if request.Method == "initialized" {
			if p.initialized != nil {
				select {
				case p.initialized <- struct{}{}:
				default:
				}
			}
			continue
		}
		if request.Method == "initialize" {
			var params executorInitialize
			if json.Unmarshal(request.Params, &params) != nil {
				return
			}
			id := "native-session"
			if params.ResumeSessionID != nil {
				id = *params.ResumeSessionID
			}
			if p.sessionID != nil {
				id = p.sessionID(request)
			}
			reply := fmt.Sprintf(`{ "id": %q, "result": {"sessionId":%q,"environmentInfo":{"shell":{"name":"bash","path":"/bin/bash"}}} }`, request.ID, id)
			if ws.Write(r.Context(), websocket.MessageText, []byte(reply)) != nil {
				return
			}
			continue
		}
		if p.handle != nil && !p.handle(r.Context(), ws, request) {
			return
		}
	}
}

func (p *peer) count(method string) int {
	p.mu.Lock()
	defer p.mu.Unlock()
	count := 0
	for _, r := range p.requests {
		if r.Method == method {
			count++
		}
	}
	return count
}

func fixture(t *testing.T, p *peer) (*Adapter, runnerexecpb.RunnerExecClient) {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(p.serve))
	t.Cleanup(server.Close)
	adapter, err := New(Config{Context: t.Context(), Admit: func(ctx context.Context, _ *executionpb.ExecutionScope, _ bool, accept func(context.Context) error) error {
		if err := ctx.Err(); err != nil {
			return err
		}
		return accept(ctx)
	}, Acquire: func(ctx context.Context) (string, error) {
		p.mu.Lock()
		acquireErr, acquireGate, acquireWait := p.acquireErr, p.acquireGate, p.acquireWait
		p.mu.Unlock()
		if acquireErr != nil {
			return "", acquireErr
		}
		if acquireGate != nil {
			if acquireWait != nil {
				select {
				case acquireWait <- struct{}{}:
				default:
				}
			}
			select {
			case <-acquireGate:
			case <-ctx.Done():
				return "", ctx.Err()
			}
		}
		return "ws" + strings.TrimPrefix(server.URL, "http"), nil
	}})
	if err != nil {
		t.Fatal(err)
	}
	listener := bufconn.Listen(4 << 20)
	grpcServer := grpc.NewServer()
	runnerexecpb.RegisterRunnerExecServer(grpcServer, adapter)
	go func() { _ = grpcServer.Serve(listener) }()
	conn, err := grpc.NewClient("passthrough:///adapter", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }), grpc.WithDisableRetry())
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close(); grpcServer.Stop(); _ = adapter.Close() })
	return adapter, runnerexecpb.NewRunnerExecClient(conn)
}

func testScope() *executionpb.ExecutionScope {
	return &executionpb.ExecutionScope{Destination: &sandboxespb.SandboxDestination{Sandbox: "sandbox", SandboxUid: "sandbox-uid", Owner: &sandboxespb.ServiceAccount{Namespace: "agents", Name: "sampler"}}, ExpectedIncarnationUid: "incarnation", SessionId: "sampler-session"}
}

func open(t *testing.T, client runnerexecpb.RunnerExecClient, scope *executionpb.ExecutionScope, resume *string) (*executionpb.OpenSessionResponse, context.CancelFunc) {
	t.Helper()
	ctx, cancel := context.WithCancel(t.Context())
	stream, err := client.OpenSession(ctx, &executionpb.OpenSessionRequest{Scope: scope, RequestId: "open", ResumeSessionId: resume})
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	response, err := stream.Recv()
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	t.Cleanup(cancel)
	return response, cancel
}

func nativeResult(ctx context.Context, ws *websocket.Conn, r nativeRequest, result string) bool {
	return ws.Write(ctx, websocket.MessageText, []byte(fmt.Sprintf(`{"id":%q,"result":%s}`, r.ID, result))) == nil
}

func completed(t *testing.T, status *executionpb.ExecutionStatus, frames []*executionpb.ExecutionNativeFrame, method string) {
	t.Helper()
	if status.GetOutcome() != executionpb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("outcome=%v", status)
	}
	if len(frames) != 2 || frames[0].Direction != "request" || frames[1].Direction != "response" {
		t.Fatalf("native frames=%v", frames)
	}
	var request nativeRequest
	if err := json.Unmarshal(frames[0].Payload, &request); err != nil || request.Method != method {
		t.Fatalf("native request=%s err=%v", frames[0].Payload, err)
	}
}

func TestTypedMappingAndExactNativeEvidence(t *testing.T) {
	p := &peer{initialized: make(chan struct{}, 1)}
	p.handle = func(ctx context.Context, ws *websocket.Conn, r nativeRequest) bool {
		results := map[string]string{"process/start": `{"processId":"p"}`, "process/read": `{"chunks":[{"seq":9,"stream":"stdout","chunk":"AP8="}],"nextSeq":10,"exited":true,"exitCode":7,"closed":true}`, "process/write": `{"status":"accepted"}`, "process/signal": `{}`, "process/terminate": `{"running":false}`, "fs/getMetadata": `{"isDirectory":false,"isFile":true,"isSymlink":false,"size":22}`, "fs/open": `{"handleId":"h"}`, "fs/readBlock": `{"chunk":"AP8=","eof":true}`, "fs/close": `{}`}
		return nativeResult(ctx, ws, r, results[r.Method])
	}
	_, client := fixture(t, p)
	scope := testScope()
	opened, _ := open(t, client, scope, nil)
	if opened.Status.Outcome != executionpb.ExecutionStatus_OUTCOME_COMPLETED || opened.ExecutionSessionId != "native-session" || opened.Shell.Path != "/bin/bash" {
		t.Fatalf("open=%v", opened)
	}
	if len(opened.NativeFrames) != 3 || !bytes.Contains(opened.NativeFrames[1].Payload, []byte(`{ "id": "open"`)) {
		t.Fatalf("initial native bytes were not preserved: %v", opened.NativeFrames)
	}
	sid := opened.ExecutionSessionId
	arg0 := "custom"
	after := uint64(8)
	started, err := client.StartProcess(t.Context(), &executionpb.StartProcessRequest{Scope: scope, ExecutionSessionId: sid, RequestId: "start", ProcessId: "p", Argv: []string{"/bin/sh", "-c", "printf x"}, Cwd: "file:///work", EnvironmentPolicy: &executionpb.ExecutionEnvironmentPolicy{Inherit: "all", IgnoreDefaultExcludes: true, Set: map[string]string{"A": "B"}}, Environment: map[string]string{"C": "D"}, Tty: true, PipeStdin: true, Arg0: &arg0})
	if err != nil {
		t.Fatal(err)
	}
	completed(t, started.Status, started.NativeFrames, "process/start")
	var start executorStart
	var startFrame nativeRequest
	_ = json.Unmarshal(started.NativeFrames[0].Payload, &startFrame)
	if err := json.Unmarshal(startFrame.Params, &start); err != nil {
		t.Fatal(err)
	}
	if start.EnvPolicy.Set["A"] != "B" || start.Env["C"] != "D" || !start.TTY || !start.PipeStdin || *start.Arg0 != "custom" {
		t.Fatalf("start mapping=%+v", start)
	}
	read, err := client.ReadProcess(t.Context(), &executionpb.ReadProcessRequest{Scope: scope, ExecutionSessionId: sid, RequestId: "read", ProcessId: "p", AfterSeq: &after, MaxBytes: 65536, WaitMs: 10})
	if err != nil {
		t.Fatal(err)
	}
	completed(t, read.Status, read.NativeFrames, "process/read")
	if len(read.Chunks) != 1 || !bytes.Equal(read.Chunks[0].Data, []byte{0, 255}) || read.NextSeq != 10 || read.GetExitCode() != 7 || !read.Closed {
		t.Fatalf("read=%v", read)
	}
	written, err := client.WriteProcess(t.Context(), &executionpb.WriteProcessRequest{Scope: scope, ExecutionSessionId: sid, RequestId: "write", ProcessId: "p", Data: []byte{0, 255}, WriteId: "write-id"})
	if err != nil {
		t.Fatal(err)
	}
	completed(t, written.Status, written.NativeFrames, "process/write")
	if written.WriteStatus != "accepted" {
		t.Fatal(written)
	}
	signaled, err := client.SignalProcess(t.Context(), &executionpb.SignalProcessRequest{Scope: scope, ExecutionSessionId: sid, RequestId: "signal", ProcessId: "p", Signal: "interrupt"})
	if err != nil {
		t.Fatal(err)
	}
	completed(t, signaled.Status, signaled.NativeFrames, "process/signal")
	terminated, err := client.TerminateProcess(t.Context(), &executionpb.TerminateProcessRequest{Scope: scope, ExecutionSessionId: sid, RequestId: "terminate", ProcessId: "p"})
	if err != nil {
		t.Fatal(err)
	}
	completed(t, terminated.Status, terminated.NativeFrames, "process/terminate")
	metadata, err := client.GetFileMetadata(t.Context(), &executionpb.GetFileMetadataRequest{Scope: scope, ExecutionSessionId: sid, RequestId: "metadata", Path: "file:///work/image.png"})
	if err != nil {
		t.Fatal(err)
	}
	completed(t, metadata.Status, metadata.NativeFrames, "fs/getMetadata")
	if metadata.Size != 22 || !metadata.IsFile {
		t.Fatal(metadata)
	}
	file, err := client.OpenFile(t.Context(), &executionpb.OpenFileRequest{Scope: scope, ExecutionSessionId: sid, RequestId: "open-file", HandleId: "h", Path: "file:///work/image.png"})
	if err != nil {
		t.Fatal(err)
	}
	completed(t, file.Status, file.NativeFrames, "fs/open")
	block, err := client.ReadFile(t.Context(), &executionpb.ReadFileRequest{Scope: scope, ExecutionSessionId: sid, RequestId: "read-file", HandleId: "h", Offset: 17, Length: 2})
	if err != nil {
		t.Fatal(err)
	}
	completed(t, block.Status, block.NativeFrames, "fs/readBlock")
	if !bytes.Equal(block.Data, []byte{0, 255}) || !block.Eof {
		t.Fatal(block)
	}
	closed, err := client.CloseFile(t.Context(), &executionpb.CloseFileRequest{Scope: scope, ExecutionSessionId: sid, RequestId: "close-file", HandleId: "h"})
	if err != nil {
		t.Fatal(err)
	}
	completed(t, closed.Status, closed.NativeFrames, "fs/close")
}

func TestEmptyValuesUseNativeJSONShapes(t *testing.T) {
	p := &peer{handle: func(ctx context.Context, ws *websocket.Conn, r nativeRequest) bool {
		if r.Method == "process/write" {
			return nativeResult(ctx, ws, r, `{"status":"accepted"}`)
		}
		return nativeResult(ctx, ws, r, `{"processId":"p"}`)
	}}
	_, client := fixture(t, p)
	scope := testScope()
	opened, _ := open(t, client, scope, nil)
	requests := []*executionpb.StartProcessRequest{
		{Scope: scope, ExecutionSessionId: opened.ExecutionSessionId, RequestId: "start-empty", ProcessId: "p", Argv: []string{"/bin/sh"}, Cwd: "file:///work", EnvironmentPolicy: &executionpb.ExecutionEnvironmentPolicy{}},
		{Scope: scope, ExecutionSessionId: opened.ExecutionSessionId, RequestId: "start-absent", ProcessId: "p", Argv: []string{"/bin/sh"}, Cwd: "file:///work"},
	}
	want := [][]byte{
		[]byte(`{"id":"start-empty","method":"process/start","params":{"processId":"p","argv":["/bin/sh"],"cwd":"file:///work","envPolicy":{"inherit":"","ignoreDefaultExcludes":false,"exclude":[],"set":{},"includeOnly":[]},"env":{},"tty":false,"pipeStdin":false,"arg0":null}}`),
		[]byte(`{"id":"start-absent","method":"process/start","params":{"processId":"p","argv":["/bin/sh"],"cwd":"file:///work","envPolicy":null,"env":{},"tty":false,"pipeStdin":false,"arg0":null}}`),
	}
	for i, request := range requests {
		response, err := client.StartProcess(t.Context(), request)
		if err != nil || response.Status.GetOutcome() != executionpb.ExecutionStatus_OUTCOME_COMPLETED {
			t.Fatalf("start %d response=%v err=%v", i, response, err)
		}
		if len(response.NativeFrames) != 2 || !bytes.Equal(response.NativeFrames[0].Payload, want[i]) {
			t.Fatalf("start %d native request=%s, want %s", i, response.NativeFrames[0].Payload, want[i])
		}
	}
	written, err := client.WriteProcess(t.Context(), &executionpb.WriteProcessRequest{Scope: scope, ExecutionSessionId: opened.ExecutionSessionId, RequestId: "write-empty", ProcessId: "p", WriteId: "w"})
	if err != nil || written.Status.GetOutcome() != executionpb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("write response=%v err=%v", written, err)
	}
	wantWrite := []byte(`{"id":"write-empty","method":"process/write","params":{"processId":"p","chunk":"","writeId":"w"}}`)
	if len(written.NativeFrames) != 2 || !bytes.Equal(written.NativeFrames[0].Payload, wantWrite) {
		t.Fatalf("write native request=%s, want %s", written.NativeFrames[0].Payload, wantWrite)
	}
}

func TestNotificationBurstUsesOpenSessionAndDoesNotPoisonUnaryRPCs(t *testing.T) {
	notification := []byte(fmt.Sprintf(`{"method":"codex/event","params":{"data":%q}}`, strings.Repeat("x", 600_000)))
	nullIDNotification := []byte(`{"id":null,"method":"codex/event","params":{"data":"null-id"}}`)
	p := &peer{handle: func(ctx context.Context, ws *websocket.Conn, r nativeRequest) bool {
		if r.Method == "process/start" {
			if err := ws.Write(ctx, websocket.MessageText, nullIDNotification); err != nil {
				return false
			}
			for range 5 {
				if err := ws.Write(ctx, websocket.MessageText, notification); err != nil {
					return false
				}
			}
			return nativeResult(ctx, ws, r, `{"processId":"p"}`)
		}
		if r.Method == "process/read" {
			return nativeResult(ctx, ws, r, `{"chunks":[],"nextSeq":0,"exited":false,"closed":false}`)
		}
		return false
	}}
	_, client := fixture(t, p)
	scope := testScope()
	ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
	defer cancel()
	stream, err := client.OpenSession(ctx, &executionpb.OpenSessionRequest{Scope: scope, RequestId: "open"})
	if err != nil {
		t.Fatal(err)
	}
	opened, err := stream.Recv()
	if err != nil || opened.Status.GetOutcome() != executionpb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("open=%v err=%v", opened, err)
	}
	notifications := make(chan *executionpb.OpenSessionResponse, 6)
	streamDone := make(chan error, 1)
	go func() {
		for range 6 {
			response, err := stream.Recv()
			if err != nil {
				streamDone <- err
				return
			}
			notifications <- response
		}
		streamDone <- nil
	}()
	started, err := client.StartProcess(ctx, &executionpb.StartProcessRequest{Scope: scope, ExecutionSessionId: opened.ExecutionSessionId, RequestId: "start", ProcessId: "p", Argv: []string{"/bin/sh"}, Cwd: "file:///work"})
	if err != nil || started.Status.GetOutcome() != executionpb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("start=%v err=%v", started, err)
	}
	if len(started.NativeFrames) != 2 {
		t.Fatalf("unary evidence includes notifications: %d frames", len(started.NativeFrames))
	}
	var transferred int
	for i := range 6 {
		var response *executionpb.OpenSessionResponse
		select {
		case response = <-notifications:
		case <-ctx.Done():
			t.Fatalf("timed out receiving notification %d: %v", i, ctx.Err())
		}
		if response.ExecutionSessionId != opened.ExecutionSessionId || response.Status != nil || len(response.NativeFrames) != 1 {
			t.Fatalf("notification response=%v", response)
		}
		frame := response.NativeFrames[0]
		want := notification
		if i == 0 {
			want = nullIDNotification
		}
		if frame.Direction != "notification" || !bytes.Equal(frame.Payload, want) {
			t.Fatalf("notification evidence changed: direction=%q payload bytes=%d", frame.Direction, len(frame.Payload))
		}
		transferred += len(frame.Payload)
	}
	if err := <-streamDone; err != nil {
		t.Fatal(err)
	}
	if transferred <= 2*maxNativeBytes {
		t.Fatalf("notification burst size=%d, want more than %d", transferred, 2*maxNativeBytes)
	}
	read, err := client.ReadProcess(ctx, &executionpb.ReadProcessRequest{Scope: scope, ExecutionSessionId: opened.ExecutionSessionId, RequestId: "read", ProcessId: "p", MaxBytes: 1})
	if err != nil || read.Status.GetOutcome() != executionpb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("healthy session read=%v err=%v", read, err)
	}
}

func TestRejectedCallAndValidation(t *testing.T) {
	p := &peer{handle: func(ctx context.Context, ws *websocket.Conn, r nativeRequest) bool {
		return ws.Write(ctx, websocket.MessageText, []byte(fmt.Sprintf(`{"id":%q,"error":{"code":-32602,"message":"unsupported process"}}`, r.ID))) == nil
	}}
	_, client := fixture(t, p)
	scope := testScope()
	opened, _ := open(t, client, scope, nil)
	read, err := client.ReadProcess(t.Context(), &executionpb.ReadProcessRequest{Scope: scope, ExecutionSessionId: opened.ExecutionSessionId, RequestId: "read", ProcessId: "p", MaxBytes: 1})
	if err != nil {
		t.Fatal(err)
	}
	if read.Status.Outcome != executionpb.ExecutionStatus_OUTCOME_REJECTED || read.Status.GetUpstreamCode() != -32602 || len(read.NativeFrames) != 2 {
		t.Fatal(read)
	}
	_, err = client.SignalProcess(t.Context(), &executionpb.SignalProcessRequest{Scope: scope, ExecutionSessionId: opened.ExecutionSessionId, RequestId: "invalid-signal", ProcessId: "p", Signal: "terminate"})
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("unsupported signal err=%v", err)
	}
	other := proto.Clone(scope).(*executionpb.ExecutionScope)
	other.SessionId = "other-session"
	_, err = client.TerminateProcess(t.Context(), &executionpb.TerminateProcessRequest{Scope: other, ExecutionSessionId: opened.ExecutionSessionId, RequestId: "cross-scope", ProcessId: "p"})
	if status.Code(err) != codes.PermissionDenied {
		t.Fatalf("scope err=%v", err)
	}
	if p.count("process/terminate") != 0 || p.count("process/read") != 1 || p.count("process/signal") != 0 {
		t.Fatal("rejected validation reached native executor")
	}
	largePath := "file://" + strings.Repeat("x", maxNativeBytes)
	large, err := client.GetFileMetadata(t.Context(), &executionpb.GetFileMetadataRequest{Scope: scope, ExecutionSessionId: opened.ExecutionSessionId, RequestId: "large-metadata", Path: largePath})
	if err != nil || large.Status.GetOutcome() != executionpb.ExecutionStatus_OUTCOME_REJECTED || len(large.NativeFrames) != 0 {
		t.Fatalf("oversize encoded native request=%v err=%v", large, err)
	}
	if p.count("fs/getMetadata") != 0 {
		t.Fatal("oversize native request was sent")
	}
}

func TestCanceledUnaryDoesNotCancelNativeCallOrReplay(t *testing.T) {
	started := make(chan struct{})
	release := make(chan struct{})
	finished := make(chan struct{})
	p := &peer{handle: func(ctx context.Context, ws *websocket.Conn, r nativeRequest) bool {
		if r.Method == "process/start" {
			close(started)
			select {
			case <-release:
			case <-ctx.Done():
				return false
			}
			ok := nativeResult(ctx, ws, r, `{"processId":"p"}`)
			close(finished)
			return ok
		}
		return nativeResult(ctx, ws, r, `{"chunks":[],"nextSeq":0,"exited":false,"closed":false}`)
	}}
	_, client := fixture(t, p)
	scope := testScope()
	opened, _ := open(t, client, scope, nil)
	ctx, cancel := context.WithCancel(t.Context())
	result := make(chan error, 1)
	go func() {
		_, err := client.StartProcess(ctx, &executionpb.StartProcessRequest{Scope: scope, ExecutionSessionId: opened.ExecutionSessionId, RequestId: "start", ProcessId: "p", Argv: []string{"/bin/sh"}, Cwd: "file:///work"})
		result <- err
	}()
	select {
	case <-started:
	case <-time.After(5 * time.Second):
		t.Fatal("native start did not arrive")
	}
	cancel()
	if err := <-result; status.Code(err) != codes.Canceled {
		t.Fatalf("canceled call=%v", err)
	}
	close(release)
	select {
	case <-finished:
	case <-time.After(5 * time.Second):
		t.Fatal("native call canceled with unary")
	}
	read, err := client.ReadProcess(t.Context(), &executionpb.ReadProcessRequest{Scope: scope, ExecutionSessionId: opened.ExecutionSessionId, RequestId: "read", ProcessId: "p", MaxBytes: 1})
	if err != nil || read.Status.Outcome != executionpb.ExecutionStatus_OUTCOME_COMPLETED {
		t.Fatalf("later read=%v err=%v", read, err)
	}
	if p.count("process/start") != 1 || p.count("process/terminate") != 0 {
		t.Fatal("native call was replayed or process termination was invented")
	}
}

func TestLostNativeReplyIsUnknownWithoutRetry(t *testing.T) {
	p := &peer{handle: func(_ context.Context, ws *websocket.Conn, r nativeRequest) bool { _ = ws.CloseNow(); return false }}
	_, client := fixture(t, p)
	scope := testScope()
	opened, _ := open(t, client, scope, nil)
	result, err := client.StartProcess(t.Context(), &executionpb.StartProcessRequest{Scope: scope, ExecutionSessionId: opened.ExecutionSessionId, RequestId: "start", ProcessId: "p", Argv: []string{"/bin/sh"}, Cwd: "file:///work"})
	if err != nil {
		t.Fatal(err)
	}
	if result.Status.Outcome != executionpb.ExecutionStatus_OUTCOME_UNKNOWN || p.count("process/start") != 1 {
		t.Fatalf("lost reply=%v native start count=%d", result, p.count("process/start"))
	}
	if !strings.Contains(result.Status.Message, "read local exec-server WebSocket frame") {
		t.Fatalf("lost reply omitted the original transport cause: %q", result.Status.Message)
	}
}

func TestBinaryNativeFrameRejectsAndPreservesEvidence(t *testing.T) {
	binaryFrame := []byte{0, 255, 1, 2}
	p := &peer{handle: func(ctx context.Context, ws *websocket.Conn, r nativeRequest) bool {
		return ws.Write(ctx, websocket.MessageBinary, binaryFrame) == nil
	}}
	_, client := fixture(t, p)
	scope := testScope()
	ctx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	stream, err := client.OpenSession(ctx, &executionpb.OpenSessionRequest{Scope: scope, RequestId: "open"})
	if err != nil {
		t.Fatal(err)
	}
	opened, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	result, err := client.ReadProcess(t.Context(), &executionpb.ReadProcessRequest{Scope: scope, ExecutionSessionId: opened.ExecutionSessionId, RequestId: "read", ProcessId: "p", MaxBytes: 1})
	if err != nil {
		t.Fatal(err)
	}
	if result.Status.GetOutcome() != executionpb.ExecutionStatus_OUTCOME_UNKNOWN || !strings.Contains(result.Status.Message, "non-text WebSocket frame") {
		t.Fatalf("binary native frame outcome=%v", result.Status)
	}
	if len(result.NativeFrames) != 1 || result.NativeFrames[0].Direction != "request" {
		t.Fatalf("unary evidence included a non-reply native frame: %v", result.NativeFrames)
	}
	terminal, err := stream.Recv()
	if err != nil || terminal.Status != nil || terminal.ExecutionSessionId != opened.ExecutionSessionId || len(terminal.NativeFrames) != 1 || terminal.NativeFrames[0].Direction != "notification" || !bytes.Equal(terminal.NativeFrames[0].Payload, binaryFrame) {
		t.Fatalf("binary terminal evidence=%v err=%v", terminal, err)
	}
	if _, err := stream.Recv(); status.Code(err) != codes.Unavailable || !strings.Contains(err.Error(), "non-text WebSocket frame") {
		t.Fatalf("binary terminal cause=%v", err)
	}
}

func TestResumeIdentityAndStreamLifetime(t *testing.T) {
	p := &peer{}
	adapter, client := fixture(t, p)
	scope := testScope()
	opened, cancel := open(t, client, scope, nil)
	cancel()
	resumed, stop := open(t, client, scope, &opened.ExecutionSessionId)
	if resumed.Status.Outcome != executionpb.ExecutionStatus_OUTCOME_COMPLETED || resumed.ExecutionSessionId != opened.ExecutionSessionId {
		t.Fatalf("resume=%v", resumed)
	}
	stop()
	deadline := time.Now().Add(5 * time.Second)
	for adapter.Active() && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	p.sessionID = func(nativeRequest) string { return "substituted-session" }
	substituted, _ := open(t, client, scope, &opened.ExecutionSessionId)
	if substituted.Status.Outcome != executionpb.ExecutionStatus_OUTCOME_UNKNOWN || substituted.ExecutionSessionId != "" {
		t.Fatalf("substituted session accepted: %v", substituted)
	}
	if p.count("process/terminate") != 0 {
		t.Fatal("stream close terminated a process")
	}
}

func TestConcurrentResumeDoesNotExceedSessionAdmissionLimit(t *testing.T) {
	p := &peer{}
	p.sessionID = func(request nativeRequest) string {
		var params executorInitialize
		if err := json.Unmarshal(request.Params, &params); err != nil {
			return ""
		}
		if params.ResumeSessionID != nil {
			return *params.ResumeSessionID
		}
		return "native-" + request.ID
	}
	_, client := fixture(t, p)
	scope := testScope()
	sessionIDs := make([]string, maxSessions)
	for i := range maxSessions {
		ctx, cancel := context.WithCancel(t.Context())
		t.Cleanup(cancel)
		stream, err := client.OpenSession(ctx, &executionpb.OpenSessionRequest{Scope: scope, RequestId: fmt.Sprintf("open-%d", i)})
		if err != nil {
			t.Fatal(err)
		}
		response, err := stream.Recv()
		if err != nil || response.Status.GetOutcome() != executionpb.ExecutionStatus_OUTCOME_COMPLETED {
			t.Fatalf("open %d response=%v err=%v", i, response, err)
		}
		sessionIDs[i] = response.ExecutionSessionId
	}

	gate := make(chan struct{})
	entered := make(chan struct{}, 1)
	p.mu.Lock()
	p.acquireGate = gate
	p.acquireWait = entered
	p.mu.Unlock()
	firstCtx, firstCancel := context.WithCancel(t.Context())
	defer firstCancel()
	first, err := client.OpenSession(firstCtx, &executionpb.OpenSessionRequest{Scope: scope, RequestId: "resume-first", ResumeSessionId: &sessionIDs[0]})
	if err != nil {
		t.Fatal(err)
	}
	firstResult := make(chan struct {
		response *executionpb.OpenSessionResponse
		err      error
	}, 1)
	go func() {
		response, err := first.Recv()
		firstResult <- struct {
			response *executionpb.OpenSessionResponse
			err      error
		}{response, err}
	}()
	select {
	case <-entered:
	case <-time.After(5 * time.Second):
		t.Fatal("first resume did not reserve its admission slot")
	}

	second, err := client.OpenSession(t.Context(), &executionpb.OpenSessionRequest{Scope: scope, RequestId: "resume-second", ResumeSessionId: &sessionIDs[0]})
	if err != nil {
		t.Fatalf("second resume stream open: %v", err)
	}
	if _, err := second.Recv(); status.Code(err) != codes.ResourceExhausted {
		close(gate)
		t.Fatalf("second concurrent resume error=%v, want ResourceExhausted", err)
	}
	close(gate)
	select {
	case result := <-firstResult:
		if result.err != nil || result.response.Status.GetOutcome() != executionpb.ExecutionStatus_OUTCOME_COMPLETED {
			t.Fatalf("first resume response=%v err=%v", result.response, result.err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("first resume did not complete after acquisition resumed")
	}
}

func TestExecutorAdmissionRefusalIsKnownRejection(t *testing.T) {
	p := &peer{acquireErr: status.Error(codes.FailedPrecondition, "Runner has no configured Codex executor")}
	adapter, client := fixture(t, p)
	response, _ := open(t, client, testScope(), nil)
	if response.Status.GetOutcome() != executionpb.ExecutionStatus_OUTCOME_REJECTED || !strings.Contains(response.Status.GetMessage(), "no configured Codex executor") {
		t.Fatalf("executor refusal outcome=%v", response.Status)
	}
	if response.ExecutionSessionId != "" || len(response.NativeFrames) != 0 || adapter.Active() {
		t.Fatalf("executor refusal created local session evidence: %v", response)
	}
}

func TestDrainAndCloseReturnWhenOpenSessionSendStalls(t *testing.T) {
	for _, closeAdapter := range []bool{false, true} {
		name := "drain"
		if closeAdapter {
			name = "close"
		}
		t.Run(name, func(t *testing.T) {
			peer := &peer{}
			server := httptest.NewServer(http.HandlerFunc(peer.serve))
			defer server.Close()
			adapter, err := New(Config{
				Context: context.Background(),
				Admit: func(ctx context.Context, _ *executionpb.ExecutionScope, _ bool, accept func(context.Context) error) error {
					return accept(ctx)
				},
				Acquire: func(context.Context) (string, error) { return "ws" + strings.TrimPrefix(server.URL, "http"), nil },
			})
			if err != nil {
				t.Fatal(err)
			}
			streamCtx, cancel := context.WithCancel(t.Context())
			defer cancel()
			stream := &blockingOpenSessionStream{ctx: streamCtx, started: make(chan struct{}), release: make(chan struct{})}
			openDone := make(chan error, 1)
			go func() {
				openDone <- adapter.OpenSession(&executionpb.OpenSessionRequest{Scope: testScope(), RequestId: "open"}, stream)
			}()
			select {
			case <-stream.started:
			case <-time.After(5 * time.Second):
				close(stream.release)
				_ = adapter.Close()
				t.Fatal("OpenSession did not reach its first response send")
			}
			operationDone := make(chan error, 1)
			if closeAdapter {
				go func() { operationDone <- adapter.Close() }()
			} else {
				drainCtx, drainCancel := context.WithTimeout(t.Context(), 2*time.Second)
				defer drainCancel()
				go func() { operationDone <- adapter.Drain(drainCtx) }()
			}
			select {
			case err := <-operationDone:
				if err != nil {
					t.Fatalf("lifecycle operation: %v", err)
				}
			case <-time.After(3 * time.Second):
				close(stream.release)
				t.Fatal("lifecycle operation wedged on gRPC stream Send")
			}
			close(stream.release)
			select {
			case <-openDone:
			case <-time.After(2 * time.Second):
				t.Fatal("OpenSession handler did not return after lifecycle cancellation")
			}
			if !closeAdapter {
				if err := adapter.Close(); err != nil {
					t.Fatal(err)
				}
			}
		})
	}
}

func TestDrainIsReversibleAndClosesHeldExecutorConnection(t *testing.T) {
	adapter, client := fixture(t, &peer{})
	scope := testScope()
	opened, _ := open(t, client, scope, nil)
	if !adapter.Active() {
		t.Fatal("open execution session is not active")
	}
	drainCtx, cancel := context.WithTimeout(t.Context(), 5*time.Second)
	defer cancel()
	if err := adapter.Drain(drainCtx); err != nil {
		t.Fatal(err)
	}
	if adapter.Active() {
		t.Fatal("drained execution session still active")
	}
	resumed, stop := open(t, client, scope, &opened.ExecutionSessionId)
	if resumed.Status.GetOutcome() != executionpb.ExecutionStatus_OUTCOME_COMPLETED || resumed.ExecutionSessionId != opened.ExecutionSessionId {
		t.Fatalf("reopened execution session=%v", resumed)
	}
	stop()
}
