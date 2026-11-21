package residentruntime

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"log/slog"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	runtimepkg "runtime"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	runtimetesting "github.com/agentydragon/ducktape/agentplane/sampler/internal/residentruntime/testing"

	residentsession "github.com/agentydragon/ducktape/agentplane/sampler/internal/residentruntime/session"

	"github.com/agentydragon/ducktape/agentplane/env/jsonrpc"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/testsupport"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"

	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/codemode"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/executor"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
	"github.com/google/uuid"
	"google.golang.org/grpc"
)

func startRuntimeForTest(t *testing.T, runtime *Runtime) (context.CancelFunc, <-chan error) {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan error, 1)
	go func() { done <- runtime.Run(ctx, nil) }()

	deadline := time.NewTimer(5 * time.Second)
	defer deadline.Stop()
	ticker := time.NewTicker(time.Millisecond)
	defer ticker.Stop()
	for {
		runtime.mu.Lock()
		started := runtime.runCtx != nil
		runtime.mu.Unlock()
		if started {
			return cancel, done
		}
		select {
		case <-deadline.C:
			cancel()
			t.Fatal("runtime did not start")
		case <-ticker.C:
		}
	}
}

// fakeCodeHost is a Code Mode host that never starts a real process.
func fakeCodeHost(t *testing.T) *residentsession.CodeModeHost {
	t.Helper()
	manager, _ := newFakeCodeModeManager(t)
	return manager
}

func assertRuntimeWaitsForGate(t *testing.T, done <-chan error, description string) {
	t.Helper()
	select {
	case err := <-done:
		t.Fatalf("runtime returned before %s completed: %v", description, err)
	case <-time.After(100 * time.Millisecond):
	}
}

func waitRuntimeDone(t *testing.T, done <-chan error) {
	t.Helper()
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("runtime returned an error: %v", err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("runtime did not stop")
	}
}

func TestRuntimeShutdownWaitsForClaimReturnAndReleasesOwnership(t *testing.T) {
	store := runtimetesting.NewStore(t, integrationDatabase)
	session := runtimetesting.OpenSessionForStore(t, store, "")
	runtime := New(store, samplersettings.Settings{MaxLiveSessions: 1, RPCTimeoutSeconds: 5, LeaseSeconds: 60}, fakeCodeHost(t))
	claimCommitted := make(chan sessionstore.Owner, 1)
	allowClaimReturn := make(chan struct{})
	runtime.claimLease = func(ctx context.Context, conversationID string, lease time.Duration) (sessionstore.Owner, error) {
		owner, err := store.Claim(ctx, conversationID, lease)
		if err == nil {
			claimCommitted <- owner
			<-allowClaimReturn
		}
		return owner, err
	}

	cancel, done := startRuntimeForTest(t, runtime)
	defer cancel()
	runtime.Wake(session.ID)

	var owner sessionstore.Owner
	select {
	case owner = <-claimCommitted:
	case <-time.After(5 * time.Second):
		t.Fatal("runtime did not commit its lease claim")
	}
	cancel()
	assertRuntimeWaitsForGate(t, done, "the committed claim to return")
	close(allowClaimReturn)
	waitRuntimeDone(t, done)

	newOwner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatalf("fresh claim failed after runtime shutdown: %v", err)
	}
	if newOwner.Generation != owner.Generation+1 {
		t.Fatalf("fresh claim generation=%d, want %d", newOwner.Generation, owner.Generation+1)
	}
	if err := store.Release(t.Context(), newOwner); err != nil {
		t.Fatal(err)
	}
}

func TestRuntimeShutdownWaitsForReleaseReturn(t *testing.T) {
	store := runtimetesting.NewStore(t, integrationDatabase)
	session := runtimetesting.OpenSessionForStore(t, store, "")
	logged := testsupport.CaptureLogs(t)
	runtime := New(store, samplersettings.Settings{MaxLiveSessions: 1, RPCTimeoutSeconds: 5, LeaseSeconds: 60}, fakeCodeHost(t))
	releaseCommitted := make(chan sessionstore.Owner, 1)
	allowReleaseReturn := make(chan struct{})
	runtime.releaseLease = func(ctx context.Context, owner sessionstore.Owner) error {
		if err := store.Release(ctx, owner); err != nil {
			return err
		}
		releaseCommitted <- owner
		<-allowReleaseReturn
		return nil
	}

	cancel, done := startRuntimeForTest(t, runtime)
	defer cancel()
	runtime.Wake(session.ID)
	// A committed claim can still return after shutdown begins, taking the stopping path.
	// Wait until the driver accepts ownership so this test exercises drain release.
	deadline := time.NewTimer(5 * time.Second)
	defer deadline.Stop()
	ticker := time.NewTicker(time.Millisecond)
	defer ticker.Stop()
	for {
		runtime.mu.Lock()
		task := runtime.tasks[session.ID]
		owned := task != nil && task.owner != nil
		runtime.mu.Unlock()
		if owned {
			break
		}
		select {
		case <-deadline.C:
			t.Fatal("runtime did not accept its lease claim")
		case <-ticker.C:
		}
	}
	cancel()

	var owner sessionstore.Owner
	select {
	case owner = <-releaseCommitted:
	case <-time.After(5 * time.Second):
		t.Fatal("runtime did not commit its lease release")
	}
	assertRuntimeWaitsForGate(t, done, "the committed release to return")
	close(allowReleaseReturn)
	waitRuntimeDone(t, done)

	exit := logged("central sampler drain releasing Session")
	// slog widens int attributes to int64.
	generation := int64(owner.Generation)
	if exit["session_id"] != session.ID || exit["generation"] != generation || exit["active_command"] != "" ||
		exit["live_cells"] != int64(0) || exit["live_processes"] != int64(0) || exit["unresolved_intents"] != int64(0) || exit["pending"] != false {
		t.Fatalf("drain exit record=%v", exit)
	}
	released := logged("central sampler lease released")
	if released["session_id"] != session.ID || released["generation"] != generation || released["reason"] != "drain" {
		t.Fatalf("lease release record=%v", released)
	}

	newOwner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatalf("fresh claim failed after runtime shutdown: %v", err)
	}
	if newOwner.Generation != owner.Generation+1 {
		t.Fatalf("fresh claim generation=%d, want %d", newOwner.Generation, owner.Generation+1)
	}
	if err := store.Release(t.Context(), newOwner); err != nil {
		t.Fatal(err)
	}
}

func TestRuntimeShutdownKeepsEffectContextAliveUntilDriverSettles(t *testing.T) {
	runtime := New(runtimetesting.NewStore(t, integrationDatabase), samplersettings.Settings{MaxLiveSessions: 1, RPCTimeoutSeconds: 1, LeaseSeconds: 60}, fakeCodeHost(t))
	seconds := 2
	runtime.settings.ShutdownDrainSeconds = &seconds
	cancel, done := startRuntimeForTest(t, runtime)
	runtime.mu.Lock()
	runtime.drivers.Add(1)
	effectCtx := runtime.runCtx
	runtime.mu.Unlock()
	cancel()
	select {
	case <-runtime.draining:
	case <-time.After(time.Second):
		t.Fatal("runtime did not enter drain")
	}
	if err := effectCtx.Err(); err != nil {
		t.Fatalf("in-flight effect was canceled before drain deadline: %v", err)
	}
	assertRuntimeWaitsForGate(t, done, "the in-flight effect to settle")
	runtime.drivers.Done()
	waitRuntimeDone(t, done)
}

func TestRuntimeDrainDeadlineCancelsUnsettledEffect(t *testing.T) {
	runtime := New(runtimetesting.NewStore(t, integrationDatabase), samplersettings.Settings{MaxLiveSessions: 1, RPCTimeoutSeconds: 1, LeaseSeconds: 60}, fakeCodeHost(t))
	seconds := 1
	runtime.settings.ShutdownDrainSeconds = &seconds
	cancel, done := startRuntimeForTest(t, runtime)
	runtime.mu.Lock()
	runtime.drivers.Add(1)
	effectCtx := runtime.runCtx
	runtime.mu.Unlock()
	go func() {
		<-effectCtx.Done()
		runtime.drivers.Done()
	}()
	cancel()
	select {
	case <-effectCtx.Done():
	case <-time.After(3 * time.Second):
		t.Fatal("drain deadline did not cancel unsettled effect")
	}
	waitRuntimeDone(t, done)
}

// drainFixture is a runtime serving one Session whose turn is mid-way through a model `exec`
// call: the fake Code Mode host answers the cell, a loopback Executor server holds the nested
// exec_command on startGate, and the fake provider answers its one sampling request with a wait
// on the live cell once providerRespond closes.
type drainFixture struct {
	store            *sessionstore.Store
	session          *sessionstore.Session
	runtime          *Runtime
	peers            func() []*runtimetesting.CodeModePeer
	hostLogDirectory string
	bindingMu        sync.Mutex
	binding          *residentsession.CodeMode
	bindingOpenErr   string
	startGate        chan struct{}
	providerCalled   chan struct{}
	providerRespond  chan struct{}
	// released carries the live work the lease release observed under the owner fence.
	released chan sessionstore.LiveWork
}

type drainLogCapture struct {
	mu  sync.Mutex
	log bytes.Buffer
}

func (c *drainLogCapture) Write(data []byte) (int, error) {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.log.Write(data)
}

func (c *drainLogCapture) snapshot() []byte {
	c.mu.Lock()
	defer c.mu.Unlock()
	return bytes.Clone(c.log.Bytes())
}

func captureDrainRuntimeLogs(t *testing.T) *drainLogCapture {
	t.Helper()
	capture := &drainLogCapture{}
	previous := slog.Default()
	standardWriter, standardFlags, standardPrefix := log.Writer(), log.Flags(), log.Prefix()
	slog.SetDefault(slog.New(slog.NewJSONHandler(capture, nil)))
	t.Cleanup(func() {
		slog.SetDefault(previous)
		log.SetOutput(standardWriter)
		log.SetFlags(standardFlags)
		log.SetPrefix(standardPrefix)
	})
	return capture
}

type capturedDrainLog struct {
	Message   string `json:"msg"`
	SessionID string `json:"session_id"`
	LiveCells int64  `json:"live_cells"`
}

func findDrainLog(t *testing.T, logs *drainLogCapture, message string) capturedDrainLog {
	t.Helper()
	scanner := bufio.NewScanner(bytes.NewReader(logs.snapshot()))
	for scanner.Scan() {
		var record capturedDrainLog
		if err := json.Unmarshal(scanner.Bytes(), &record); err != nil {
			t.Fatalf("decode runtime log record: %v", err)
		}
		if record.Message == message {
			return record
		}
	}
	if err := scanner.Err(); err != nil {
		t.Fatalf("read runtime log records: %v", err)
	}
	t.Fatalf("no runtime log record %q", message)
	return capturedDrainLog{}
}

type drainOwnerSnapshot struct {
	ConversationID string `json:"conversation_id"`
	Generation     int    `json:"generation"`
}

type drainFailureSnapshot struct {
	CapturedAt       time.Time                    `json:"captured_at"`
	Reason           string                       `json:"reason"`
	SessionID        string                       `json:"session_id"`
	Owner            *drainOwnerSnapshot          `json:"owner,omitempty"`
	BindingError     string                       `json:"binding_error,omitempty"`
	BindingOpenError string                       `json:"binding_open_error,omitempty"`
	Database         map[string][]json.RawMessage `json:"database"`
	Errors           []string                     `json:"capture_errors,omitempty"`
}

// captureFailureDiagnostics runs from a test defer registered after defer cancel, so it retains
// the live binding, database rows, host stderr, and goroutines before deferred runtime cleanup.
func (f *drainFixture) captureFailureDiagnostics(t *testing.T, logs *drainLogCapture) {
	t.Helper()
	if !t.Failed() {
		return
	}
	f.writeDrainDiagnostics(t, logs)
}

func (f *drainFixture) writeDrainDiagnostics(t *testing.T, logs *drainLogCapture) string {
	t.Helper()
	outputDirectory := os.Getenv("TEST_UNDECLARED_OUTPUTS_DIR")
	if outputDirectory == "" {
		outputDirectory = t.TempDir()
	}
	if err := os.MkdirAll(outputDirectory, 0o750); err != nil {
		t.Errorf("create diagnostics output directory: %v", err)
		return outputDirectory
	}
	testName := strings.NewReplacer("/", "_", " ", "_").Replace(t.Name())
	write := func(suffix string, data []byte) {
		path := filepath.Join(outputDirectory, testName+suffix)
		if err := os.WriteFile(path, data, 0o640); err != nil {
			t.Errorf("write drain diagnostics artifact %s: %v", path, err)
		}
	}
	write("-runtime.jsonl", logs.snapshot())
	captureErrors := []string{}
	if hostLog, err := os.ReadFile(filepath.Join(f.hostLogDirectory, "code-mode-host.log")); err != nil {
		// The host can fail before it creates stderr output; retain that fact in the snapshot.
		captureErrors = append(captureErrors, fmt.Sprintf("read Code Mode host log: %v", err))
	} else {
		write("-code-mode-host.log", hostLog)
	}
	stack := make([]byte, 4<<20)
	stackSize := runtimepkg.Stack(stack, true)
	write("-goroutines.txt", stack[:stackSize])

	snapshot := drainFailureSnapshot{
		CapturedAt: time.Now().UTC(), Reason: "captured before deferred runtime cleanup", SessionID: f.session.ID,
		Database: map[string][]json.RawMessage{},
	}
	f.captureState(t, &snapshot)
	snapshot.Errors = append(snapshot.Errors, captureErrors...)
	encoded, err := json.MarshalIndent(snapshot, "", "  ")
	if err != nil {
		t.Errorf("marshal drain failure snapshot: %v", err)
		return outputDirectory
	}
	write("-database.json", encoded)
	return outputDirectory
}

func (f *drainFixture) captureState(t *testing.T, snapshot *drainFailureSnapshot) {
	f.runtime.mu.Lock()
	var owner *sessionstore.Owner
	if task := f.runtime.tasks[f.session.ID]; task != nil && task.owner != nil {
		value := *task.owner
		owner = &value
	}
	f.runtime.mu.Unlock()
	f.bindingMu.Lock()
	binding := f.binding
	snapshot.BindingOpenError = f.bindingOpenErr
	f.bindingMu.Unlock()
	if binding != nil {
		if err := binding.Err(); err != nil {
			snapshot.BindingError = err.Error()
		}
	}
	if owner != nil {
		snapshot.Owner = &drainOwnerSnapshot{ConversationID: owner.ConversationID, Generation: owner.Generation}
	}
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	queries := []struct {
		name  string
		query string
		args  []any
	}{
		{"conversation", `SELECT to_jsonb(c) FROM sampler_conversation c WHERE c.id=$1`, []any{f.session.ConversationID}},
		{"session", `SELECT to_jsonb(s) FROM sampler_session s WHERE s.id=$1`, []any{f.session.ID}},
		{"intents", `SELECT to_jsonb(i) FROM sampler_intent i WHERE i.conversation_id=$1 ORDER BY i.id LIMIT 100`, []any{f.session.ConversationID}},
		{"cells", `SELECT to_jsonb(c) FROM sampler_cell c WHERE c.conversation_id=$1 ORDER BY c.generation,c.id LIMIT 100`, []any{f.session.ConversationID}},
		{"processes", `SELECT to_jsonb(p) FROM sampler_process p WHERE p.conversation_id=$1 ORDER BY p.generation,p.id LIMIT 100`, []any{f.session.ConversationID}},
		{"commands", `SELECT to_jsonb(c) FROM sampler_command c WHERE c.session_id=$1 ORDER BY c.id LIMIT 100`, []any{f.session.ID}},
		{"events", `SELECT to_jsonb(e) FROM (SELECT * FROM sampler_event WHERE session_id=$1 ORDER BY sequence DESC LIMIT 100) e ORDER BY sequence`, []any{f.session.ID}},
		{"model_requests", `SELECT to_jsonb(r) FROM sampler_model_request r WHERE r.conversation_id=$1 ORDER BY r.position DESC LIMIT 20`, []any{f.session.ConversationID}},
		// Each fixture has an isolated database; include referenced content, not only context nodes.
		{"contexts", `SELECT to_jsonb(c) FROM sampler_context c ORDER BY c.item_count,c.id LIMIT 100`, nil},
		{"context_content", `SELECT to_jsonb(c) FROM sampler_content c ORDER BY c.id LIMIT 1000`, nil},
	}
	for _, query := range queries {
		f.captureRows(ctx, snapshot, query.name, query.query, query.args...)
	}
}

func (f *drainFixture) captureRows(ctx context.Context, snapshot *drainFailureSnapshot, name, query string, args ...any) {
	rows, err := f.store.DB().QueryContext(ctx, query, args...)
	if err != nil {
		snapshot.Errors = append(snapshot.Errors, fmt.Sprintf("query %s rows: %v", name, err))
		return
	}
	defer rows.Close()
	for rows.Next() {
		var wire []byte
		if err := rows.Scan(&wire); err != nil {
			snapshot.Errors = append(snapshot.Errors, fmt.Sprintf("scan %s row: %v", name, err))
			return
		}
		snapshot.Database[name] = append(snapshot.Database[name], bytes.Clone(wire))
	}
	if err := rows.Err(); err != nil {
		snapshot.Errors = append(snapshot.Errors, fmt.Sprintf("iterate %s rows: %v", name, err))
	}
}

type drainExecutorServer struct {
	executorpb.UnimplementedExecutorServer
	startGate <-chan struct{}
}

func (s *drainExecutorServer) OpenSession(_ *executorpb.OpenSessionRequest, stream executorpb.Executor_OpenSessionServer) error {
	if err := stream.Send(&executorpb.OpenSessionResponse{
		Status:             &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_COMPLETED},
		ExecutionSessionId: "drain-executor-session",
		IncarnationUid:     "drain-executor-incarnation",
		Shell:              &executorpb.ExecutionShell{Name: "bash", Path: "/bin/bash"},
	}); err != nil {
		return err
	}
	<-stream.Context().Done()
	return stream.Context().Err()
}

func (s *drainExecutorServer) StartProcess(ctx context.Context, request *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
	select {
	case <-s.startGate:
	case <-ctx.Done():
		return nil, ctx.Err()
	}
	return &executorpb.StartProcessResponse{
		Status:    &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_COMPLETED},
		ProcessId: request.GetProcessId(),
	}, nil
}

func (*drainExecutorServer) ReadProcess(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
	code := int32(0)
	return &executorpb.ReadProcessResponse{
		Status:   &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_COMPLETED},
		Exited:   true,
		Closed:   true,
		ExitCode: &code,
		NextSeq:  1,
	}, nil
}

func (*drainExecutorServer) TerminateProcess(context.Context, *executorpb.TerminateProcessRequest) (*executorpb.TerminateProcessResponse, error) {
	return &executorpb.TerminateProcessResponse{Status: &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_COMPLETED}}, nil
}

func newDrainFixture(t *testing.T, drainSeconds int) *drainFixture {
	t.Helper()
	store, session, seed := runtimetesting.ClaimedSession(t, integrationDatabase)
	runtimetesting.AdmitInput(t, store, session.ID, "turn", "run the cell")
	if _, err := store.NextCommand(t.Context(), seed, session.ID); err != nil {
		t.Fatal(err)
	}
	runtimetesting.AppendModelToolCall(t, store, seed, protocol.ToolInvocation{
		Encoding: protocol.ExecEncodingJSON, Name: "exec", CallID: "exec-call", Input: `{"code":"await tools.exec_command({cmd:'true'})","yield_time_ms":10}`,
	})
	if err := store.Release(t.Context(), seed); err != nil {
		t.Fatal(err)
	}

	f := &drainFixture{
		store: store, session: session, startGate: make(chan struct{}),
		providerCalled: make(chan struct{}, 1), providerRespond: make(chan struct{}), released: make(chan sessionstore.LiveWork, 1),
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		select {
		case f.providerCalled <- struct{}{}:
		default:
		}
		select {
		case <-f.providerRespond:
		case <-r.Context().Done():
			return
		}
		var cellID string
		if err := store.DB().QueryRowContext(r.Context(), `SELECT id FROM sampler_cell WHERE conversation_id=$1 AND status='live'`, session.ConversationID).Scan(&cellID); err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		item := responses.Item{Type: responses.ItemFunctionCall, ID: "wait-item", CallID: "wait-call", Name: "exec", Arguments: fmt.Sprintf(`{"cell_id":%q}`, cellID)}
		w.Header().Set("Content-Type", "text/event-stream")
		for _, event := range []any{
			responses.Event{Type: responses.EventOutputItemAdded, Item: &item},
			responses.Event{Type: responses.EventOutputItemDone, Item: &item},
			map[string]any{"type": "response.completed", "response": map[string]any{"id": "resp-wait", "status": "completed"}},
		} {
			wire, err := json.Marshal(event)
			if err != nil {
				t.Error(err)
				return
			}
			fmt.Fprintf(w, "data: %s\n\n", wire)
		}
	}))
	t.Cleanup(server.Close)

	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	executorServer := grpc.NewServer()
	executorpb.RegisterExecutorServer(executorServer, &drainExecutorServer{startGate: f.startGate})
	go func() { _ = executorServer.Serve(listener) }()
	t.Cleanup(func() {
		executorServer.Stop()
		_ = listener.Close()
	})
	settings := samplersettings.Settings{
		MaxLiveSessions: 1, RPCTimeoutSeconds: 5, LeaseSeconds: 60, ShutdownDrainSeconds: &drainSeconds,
		DirectExecutorTarget: listener.Addr().String(), DirectExecutorInsecureLocaltest: true,
		Connections: map[string]samplersettings.ConnectionConfig{"provider": {BaseURL: server.URL, Auth: samplersettings.AuthConfig{Type: samplersettings.AuthTypeBearer, Credential: "fixture-credential"}}},
		Models: map[string]samplersettings.ModelConfig{"fixture-model": {
			Connection: "provider", ProviderModel: "fixture-model", API: protocol.APIStyleResponses, Responses: responses.Config{Dialect: responses.DialectStandard},
			ExecEncoding: protocol.ExecEncodingJSON, ReasoningEfforts: []string{""},
		}},
	}
	manager, peers, hostLogDirectory := newFakeCodeModeManagerWithLogDirectory(t, 2*time.Second, 300*time.Second)
	manager = residentsession.NewCodeModeHost(manager.Manager, store, nil)
	f.runtime = New(store, settings, manager)
	f.peers = peers
	f.hostLogDirectory = hostLogDirectory
	openCodeSession := f.runtime.openCodeSession
	f.runtime.openCodeSession = func(ctx context.Context, conversationID string, owner sessionstore.Owner, executor *executor.Executor) (*residentsession.CodeMode, error) {
		code, err := openCodeSession(ctx, conversationID, owner, executor)
		f.bindingMu.Lock()
		defer f.bindingMu.Unlock()
		if err != nil && f.bindingOpenErr == "" {
			f.bindingOpenErr = err.Error()
		}
		if code != nil {
			f.binding = code
		}
		return code, err
	}
	f.runtime.releaseLease = func(ctx context.Context, owner sessionstore.Owner) error {
		work, err := store.LiveWork(ctx, owner)
		if err != nil {
			return err
		}
		f.released <- work
		return store.Release(ctx, owner)
	}
	return f
}

func (f *drainFixture) peer(t *testing.T) *runtimetesting.CodeModePeer {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for len(f.peers()) == 0 {
		if time.Now().After(deadline) {
			t.Fatal("fake Code Mode host did not start")
		}
		time.Sleep(time.Millisecond)
	}
	return f.peers()[0]
}

func sendNestedExecCommand(t *testing.T, peer *runtimetesting.CodeModePeer, codeSession string, id int64) {
	t.Helper()
	if err := peer.Send(codemode.Frame{
		Type: codemode.DelegateRequest, ID: int64Pointer(id), SessionID: codeSession,
		Request: &codemode.Delegate{Type: "tool/invoke", CellID: "cell-1", Invocation: codemode.ToolInvocation{
			CellID: "cell-1", ToolName: codemode.ToolName{Name: "exec_command"}, ToolKind: "function", Input: json.RawMessage(`{"cmd":"true","yield_time_ms":10}`),
		}},
	}); err != nil {
		t.Fatal(err)
	}
}

func yieldCell(t *testing.T, peer *runtimetesting.CodeModePeer, executeID int64) {
	t.Helper()
	started, err := json.Marshal(codemode.HostResponse{Type: "execution/started", CellID: "cell-1"})
	if err != nil {
		t.Fatal(err)
	}
	if err := peer.Send(codemode.Frame{Type: codemode.Response, ID: int64Pointer(executeID), Result: &codemode.WireResult{Status: "ok", Value: started}}); err != nil {
		t.Fatal(err)
	}
	observation := json.RawMessage(`{"Yielded":{"cell_id":"cell-1","content_items":[]}}`)
	if err := peer.Send(codemode.Frame{Type: codemode.InitialResponse, ID: int64Pointer(executeID), Result: &codemode.WireResult{Status: "ok", Value: observation}}); err != nil {
		t.Fatal(err)
	}
}

func completeCell(t *testing.T, peer *runtimetesting.CodeModePeer, codeSession string, waitID int64) {
	t.Helper()
	completed, err := json.Marshal(codemode.HostResponse{
		Type: "wait/completed", SessionID: codeSession,
		Outcome: json.RawMessage(`{"LiveCell":{"Result":{"cell_id":"cell-1","content_items":[{"type":"input_text","text":"done"}]}}}`),
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := peer.Send(codemode.Frame{Type: codemode.Response, ID: int64Pointer(waitID), Result: &codemode.WireResult{Status: "ok", Value: completed}}); err != nil {
		t.Fatal(err)
	}
}

func receiveDelegateResponse(t *testing.T, peer *runtimetesting.CodeModePeer, id int64) {
	t.Helper()
	deadline := time.After(3 * time.Second)
	for {
		select {
		case fields := <-peer.Frames:
			var frame struct {
				Type string `json:"type"`
			}
			if err := jsonrpc.UnmarshalObject(runtimetesting.MarshalFields(t, fields), &frame); err != nil {
				t.Fatal(err)
			}
			if frame.Type == "delegate/response" {
				runtimetesting.AssertDelegateResponse(t, fields, id, "ok")
				return
			}
		case <-deadline:
			t.Fatalf("nested tool %d did not respond", id)
		}
	}
}

// drainWithDispatchedNestedCommand starts the fixture, reaches the incident state, begins the
// drain while the provider request is in flight, then lets the provider answer with a wait on
// the live cell. It returns once the owner has asked the host to wait on the cell.
func (f *drainFixture) drainWithDispatchedNestedCommand(t *testing.T, cancel context.CancelFunc) (*runtimetesting.CodeModePeer, string, int64) {
	t.Helper()
	f.runtime.Wake(f.session.ID)
	peer := f.peer(t)
	execute := runtimetesting.ReceiveCodeOperationRequest(t, peer, "", "session/execute")
	sendNestedExecCommand(t, peer, execute.SessionID, 9001)
	yieldCell(t, peer, execute.ID)
	select {
	case <-f.providerCalled:
	case <-time.After(5 * time.Second):
		t.Fatal("the owner did not sample the model after the cell yielded")
	}
	cancel()
	select {
	case <-f.runtime.draining:
	case <-time.After(time.Second):
		t.Fatal("runtime did not enter drain")
	}
	close(f.providerRespond)
	wait := runtimetesting.ReceiveCodeOperationRequest(t, peer, execute.SessionID, "session/wait")
	return peer, execute.SessionID, wait.ID
}

func TestRuntimeDrainServesLiveCellUntilNestedCommandSettles(t *testing.T) {
	f := newDrainFixture(t, 20)
	logs := captureDrainRuntimeLogs(t)
	cancel, done := startRuntimeForTest(t, f.runtime)
	defer cancel()
	defer f.captureFailureDiagnostics(t, logs)
	peer, codeSession, waitID := f.drainWithDispatchedNestedCommand(t, cancel)
	close(f.startGate)
	receiveDelegateResponse(t, peer, 9001)
	completeCell(t, peer, codeSession, waitID)
	waitRuntimeDone(t, done)

	released := <-f.released
	if released != (sessionstore.LiveWork{}) {
		t.Fatalf("drain released the Session with live work: %+v", released)
	}
	continued := findDrainLog(t, logs, "central sampler drain keeps serving Session with live work")
	if continued.SessionID != f.session.ID || continued.LiveCells != int64(1) {
		t.Fatalf("drain continuation record=%v", continued)
	}
	successor, err := f.store.Claim(t.Context(), f.session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	runtimetesting.AssertNoUnresolvedNestedIntents(t, f.store, successor)
	if changed, err := f.store.ReconcileCodeHostLoss(t.Context(), successor, uuid.NewString(), protocol.ExecEncodingJSON); err != nil || !changed {
		t.Fatalf("successor on a new host: changed=%v err=%v", changed, err)
	}
	command, err := f.store.NextCommand(t.Context(), successor, f.session.ID)
	if err != nil || command == nil || command.ID != "turn" {
		t.Fatalf("successor did not resume the committed command: command=%+v err=%v", command, err)
	}
}

func TestRuntimeDrainDeadlineCancelsUnsettledCellAndSuccessorSettles(t *testing.T) {
	f := newDrainFixture(t, 1)
	logs := captureDrainRuntimeLogs(t)
	cancel, done := startRuntimeForTest(t, f.runtime)
	defer cancel()
	defer f.captureFailureDiagnostics(t, logs)
	// Neither the nested exec_command nor the wait on the cell ever completes.
	f.drainWithDispatchedNestedCommand(t, cancel)
	waitRuntimeDone(t, done)

	released := <-f.released
	if released.LiveCells != 1 || released.UnresolvedIntents == 0 {
		t.Fatalf("deadline release lost the unsettled evidence: %+v", released)
	}
	successor, err := f.store.Claim(t.Context(), f.session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if changed, err := f.store.ReconcileCodeHostLoss(t.Context(), successor, uuid.NewString(), protocol.ExecEncodingJSON); err != nil || !changed {
		t.Fatalf("successor did not settle the lost cell: changed=%v err=%v", changed, err)
	}
	runtimetesting.AssertNoUnresolvedNestedIntents(t, f.store, successor)
	work, err := f.store.LiveWork(t.Context(), successor)
	if err != nil || work != (sessionstore.LiveWork{}) {
		t.Fatalf("live work after settlement=%+v err=%v", work, err)
	}
	conversation, err := f.store.Read(t.Context(), successor)
	if err != nil {
		t.Fatal(err)
	}
	if calls, err := conversation.Transcript.PendingCalls(); err != nil || len(calls) != 0 {
		t.Fatalf("the model's wait call was left unanswered: calls=%v err=%v", calls, err)
	}
	contextWire, err := json.Marshal(conversation.Context())
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(contextWire, []byte("exec_command")) {
		t.Fatalf("continuation does not name the nested call with unknown outcome: %s", contextWire)
	}
	command, err := f.store.NextCommand(t.Context(), successor, f.session.ID)
	if err != nil || command == nil || command.ID != "turn" {
		t.Fatalf("successor cannot continue the turn: command=%+v err=%v", command, err)
	}
}

func TestDrainDiagnosticsRetainBindingAndDatabaseAfterDriverRelease(t *testing.T) {
	f := newDrainFixture(t, 1)
	outputs := t.TempDir()
	t.Setenv("TEST_UNDECLARED_OUTPUTS_DIR", outputs)
	logs := captureDrainRuntimeLogs(t)
	// Keep the failed binding available for inspection instead of starting a successor driver.
	var claimed atomic.Bool
	claim := f.runtime.claimLease
	f.runtime.claimLease = func(ctx context.Context, conversationID string, lease time.Duration) (sessionstore.Owner, error) {
		if !claimed.CompareAndSwap(false, true) {
			return sessionstore.Owner{}, context.Canceled
		}
		return claim(ctx, conversationID, lease)
	}
	releaseCompleted := make(chan struct{})
	release := f.runtime.releaseLease
	f.runtime.releaseLease = func(ctx context.Context, owner sessionstore.Owner) error {
		err := release(ctx, owner)
		close(releaseCompleted)
		return err
	}
	cancel, done := startRuntimeForTest(t, f.runtime)
	defer cancel()
	f.runtime.Wake(f.session.ID)
	peer := f.peer(t)
	runtimetesting.ReceiveCodeOperationRequest(t, peer, "", "session/execute")
	if err := peer.Close(); err != nil {
		t.Fatalf("close fake Code Mode peer: %v", err)
	}
	select {
	case <-f.released:
	case <-time.After(5 * time.Second):
		t.Fatal("runtime did not release the failed binding")
	}
	select {
	case <-releaseCompleted:
	case <-time.After(5 * time.Second):
		t.Fatal("runtime did not complete the owner release")
	}
	select {
	case err := <-done:
		t.Fatalf("runtime stopped before diagnostic capture: %v", err)
	default:
	}

	outputDirectory := f.writeDrainDiagnostics(t, logs)
	cancel()
	waitRuntimeDone(t, done)
	testName := strings.NewReplacer("/", "_", " ", "_").Replace(t.Name())
	readArtifact := func(suffix string) []byte {
		t.Helper()
		data, err := os.ReadFile(filepath.Join(outputDirectory, testName+suffix))
		if err != nil {
			t.Fatalf("read diagnostic artifact %s: %v", suffix, err)
		}
		return data
	}
	if got := string(readArtifact("-runtime.jsonl")); !strings.Contains(got, `"msg":"central sampler execution stopped"`) {
		t.Fatal("runtime log artifact omitted the driver error")
	}
	_ = readArtifact("-code-mode-host.log")
	if got := string(readArtifact("-goroutines.txt")); !strings.Contains(got, "goroutine ") {
		t.Fatal("goroutine stack artifact has no goroutine records")
	}
	var snapshot struct {
		BindingError string                       `json:"binding_error"`
		Database     map[string][]json.RawMessage `json:"database"`
		Errors       []string                     `json:"capture_errors"`
	}
	if err := json.Unmarshal(readArtifact("-database.json"), &snapshot); err != nil {
		t.Fatalf("decode database snapshot: %v", err)
	}
	if len(snapshot.Errors) != 0 {
		t.Fatalf("database snapshot had capture errors: %v", snapshot.Errors)
	}
	if snapshot.BindingError == "" {
		t.Fatal("database snapshot omitted the Code Mode binding error")
	}
	for _, table := range []string{"conversation", "session", "intents", "events"} {
		if len(snapshot.Database[table]) == 0 {
			t.Fatalf("database snapshot artifact has no %s rows", table)
		}
	}
	var conversation map[string]any
	if err := json.Unmarshal(snapshot.Database["conversation"][0], &conversation); err != nil {
		t.Fatalf("decode conversation row: %v", err)
	}
	if conversation["owner"] != nil {
		t.Fatalf("conversation owner is still set after driver release: %v", conversation["owner"])
	}
}

func int64Pointer(value int64) *int64 { return &value }

func newFakeCodeModeManager(t *testing.T, acknowledge ...string) (*residentsession.CodeModeHost, func() []*runtimetesting.CodeModePeer) {
	t.Helper()
	manager, peers, _ := newFakeCodeModeManagerWithLogDirectory(t, 2*time.Second, 300*time.Second, acknowledge...)
	return manager, peers
}

func newFakeCodeModeManagerWithLogDirectory(t *testing.T, rpcTimeout, observationTimeout time.Duration, acknowledge ...string) (*residentsession.CodeModeHost, func() []*runtimetesting.CodeModePeer, string) {
	t.Helper()
	manager, peers, directory := runtimetesting.NewCodeModeManager(t, rpcTimeout, observationTimeout, acknowledge...)
	return residentsession.NewCodeModeHost(manager, nil, nil), peers, directory
}
