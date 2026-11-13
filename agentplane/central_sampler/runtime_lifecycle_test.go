package centralsampler

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/codemode"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/responses"
	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/settings"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	"github.com/google/uuid"
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
func fakeCodeHost(t *testing.T) *CodeModeHost {
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
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	runtime := NewRuntime(store, samplersettings.Settings{MaxLiveSessions: 1, RPCTimeoutSeconds: 5, LeaseSeconds: 60}, fakeCodeHost(t))
	claimCommitted := make(chan Owner, 1)
	allowClaimReturn := make(chan struct{})
	runtime.claimLease = func(ctx context.Context, conversationID string, lease time.Duration) (Owner, error) {
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

	var owner Owner
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
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	logged := captureLogs(t)
	runtime := NewRuntime(store, samplersettings.Settings{MaxLiveSessions: 1, RPCTimeoutSeconds: 5, LeaseSeconds: 60}, fakeCodeHost(t))
	releaseCommitted := make(chan Owner, 1)
	allowReleaseReturn := make(chan struct{})
	runtime.releaseLease = func(ctx context.Context, owner Owner) error {
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

	var owner Owner
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
	if released["session_id"] != session.ID || released["generation"] != generation || released["reason"] != string(releaseDrain) {
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
	runtime := NewRuntime(newTestStore(t), samplersettings.Settings{MaxLiveSessions: 1, RPCTimeoutSeconds: 1, LeaseSeconds: 60}, fakeCodeHost(t))
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
	runtime := NewRuntime(newTestStore(t), samplersettings.Settings{MaxLiveSessions: 1, RPCTimeoutSeconds: 1, LeaseSeconds: 60}, fakeCodeHost(t))
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
// call: the fake Code Mode host answers the cell, the fake executor holds the cell's nested
// exec_command on startGate, and the fake provider answers its one sampling request with a wait
// on the live cell once providerRespond closes.
type drainFixture struct {
	store           *Store
	session         *Session
	runtime         *Runtime
	peers           func() []*fakeCodeModePeer
	startGate       chan struct{}
	providerCalled  chan struct{}
	providerRespond chan struct{}
	// released carries the live work the lease release observed under the owner fence.
	released chan LiveWork
}

func newDrainFixture(t *testing.T, drainSeconds int) *drainFixture {
	t.Helper()
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	seed, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.Admit(t.Context(), session.ID, &commandpb.Command{
		CommandId: "turn", Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: "run the cell"}},
	}, "fixture-account", nil); err != nil {
		t.Fatal(err)
	}
	if _, err := store.NextCommand(t.Context(), seed, session.ID); err != nil {
		t.Fatal(err)
	}
	appendModelToolCall(t, store, seed, protocol.ToolInvocation{
		Encoding: protocol.ExecEncodingJSON, Name: "exec", CallID: "exec-call", Input: `{"code":"await tools.exec_command({cmd:'true'})","yield_time_ms":10}`,
	})
	if err := store.Release(t.Context(), seed); err != nil {
		t.Fatal(err)
	}

	f := &drainFixture{
		store: store, session: session, startGate: make(chan struct{}),
		providerCalled: make(chan struct{}, 1), providerRespond: make(chan struct{}), released: make(chan LiveWork, 1),
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

	client := &fakeExecutionClient{}
	client.start = func(ctx context.Context, request *executorpb.StartProcessRequest) (*executorpb.StartProcessResponse, error) {
		select {
		case <-f.startGate:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
		return &executorpb.StartProcessResponse{Status: completedExecutionStatus(), ProcessId: request.ProcessId}, nil
	}
	client.read = func(context.Context, *executorpb.ReadProcessRequest) (*executorpb.ReadProcessResponse, error) {
		return &executorpb.ReadProcessResponse{Status: completedExecutionStatus(), Chunks: []*executorpb.ExecutionOutputChunk{}, Exited: true, Closed: true, ExitCode: ptrInt32(0), NextSeq: 1}, nil
	}
	settings := samplersettings.Settings{
		MaxLiveSessions: 1, RPCTimeoutSeconds: 5, LeaseSeconds: 60, TurnTimeoutSeconds: 30, ShutdownDrainSeconds: &drainSeconds,
		Connections: map[string]samplersettings.ConnectionConfig{"provider": {BaseURL: server.URL, Auth: samplersettings.AuthConfig{Type: samplersettings.AuthTypeBearer, Credential: "fixture-credential"}}},
		Models: map[string]samplersettings.ModelConfig{"fixture-model": {
			Connection: "provider", ProviderModel: "fixture-model", API: protocol.APIStyleResponses, Responses: responses.Config{Dialect: responses.DialectStandard},
			ExecEncoding: protocol.ExecEncodingJSON, ReasoningEfforts: []string{""},
		}},
	}
	manager, peers := newFakeCodeModeManager(t)
	manager.store = store
	f.runtime = NewRuntime(store, settings, manager)
	f.peers = peers
	f.runtime.newExecutor = func(_ context.Context, store *Store, owner Owner, _ *Session, _ samplersettings.Settings) (*Executor, func() error, error) {
		executor := newFakeExecutor(t, store, owner, client)
		executor.environment = map[string]string{}
		return executor, func() error { return nil }, nil
	}
	f.runtime.releaseLease = func(ctx context.Context, owner Owner) error {
		work, err := store.LiveWork(ctx, owner)
		if err != nil {
			return err
		}
		f.released <- work
		return store.Release(ctx, owner)
	}
	return f
}

func (f *drainFixture) peer(t *testing.T) *fakeCodeModePeer {
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

func sendNestedExecCommand(t *testing.T, peer *fakeCodeModePeer, codeSession string, id int64) {
	t.Helper()
	if err := peer.send(codemode.Frame{
		Type: codemode.DelegateRequest, ID: int64Pointer(id), SessionID: codeSession,
		Request: &codemode.Delegate{Type: "tool/invoke", CellID: "cell-1", Invocation: codemode.ToolInvocation{
			CellID: "cell-1", ToolName: codemode.ToolName{Name: "exec_command"}, ToolKind: "function", Input: json.RawMessage(`{"cmd":"true","yield_time_ms":10}`),
		}},
	}); err != nil {
		t.Fatal(err)
	}
}

func yieldCell(t *testing.T, peer *fakeCodeModePeer, executeID int64) {
	t.Helper()
	started, err := json.Marshal(codemode.HostResponse{Type: "execution/started", CellID: "cell-1"})
	if err != nil {
		t.Fatal(err)
	}
	if err := peer.send(codemode.Frame{Type: codemode.Response, ID: int64Pointer(executeID), Result: &codemode.WireResult{Status: "ok", Value: started}}); err != nil {
		t.Fatal(err)
	}
	observation := json.RawMessage(`{"Yielded":{"cell_id":"cell-1","content_items":[]}}`)
	if err := peer.send(codemode.Frame{Type: codemode.InitialResponse, ID: int64Pointer(executeID), Result: &codemode.WireResult{Status: "ok", Value: observation}}); err != nil {
		t.Fatal(err)
	}
}

func completeCell(t *testing.T, peer *fakeCodeModePeer, codeSession string, waitID int64) {
	t.Helper()
	completed, err := json.Marshal(codemode.HostResponse{
		Type: "wait/completed", SessionID: codeSession,
		Outcome: json.RawMessage(`{"LiveCell":{"Result":{"cell_id":"cell-1","content_items":[{"type":"input_text","text":"done"}]}}}`),
	})
	if err != nil {
		t.Fatal(err)
	}
	if err := peer.send(codemode.Frame{Type: codemode.Response, ID: int64Pointer(waitID), Result: &codemode.WireResult{Status: "ok", Value: completed}}); err != nil {
		t.Fatal(err)
	}
}

func receiveDelegateResponse(t *testing.T, peer *fakeCodeModePeer, id int64) {
	t.Helper()
	deadline := time.After(3 * time.Second)
	for {
		select {
		case fields := <-peer.frames:
			var frame struct {
				Type string `json:"type"`
			}
			if err := protocol.DecodeObject(mustMarshalFields(t, fields), &frame); err != nil {
				t.Fatal(err)
			}
			if frame.Type == "delegate/response" {
				assertDelegateResponse(t, fields, id, "ok")
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
func (f *drainFixture) drainWithDispatchedNestedCommand(t *testing.T, cancel context.CancelFunc) (*fakeCodeModePeer, string, int64) {
	t.Helper()
	f.runtime.Wake(f.session.ID)
	peer := f.peer(t)
	execute := receiveCodeOperationRequest(t, peer, "", "session/execute")
	sendNestedExecCommand(t, peer, execute.sessionID, 9001)
	yieldCell(t, peer, execute.id)
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
	wait := receiveCodeOperationRequest(t, peer, execute.sessionID, "session/wait")
	return peer, execute.sessionID, wait.id
}

func TestRuntimeDrainServesLiveCellUntilNestedCommandSettles(t *testing.T) {
	f := newDrainFixture(t, 20)
	logged := captureLogs(t)
	cancel, done := startRuntimeForTest(t, f.runtime)
	defer cancel()
	peer, codeSession, waitID := f.drainWithDispatchedNestedCommand(t, cancel)
	close(f.startGate)
	receiveDelegateResponse(t, peer, 9001)
	completeCell(t, peer, codeSession, waitID)
	waitRuntimeDone(t, done)

	released := <-f.released
	if released != (LiveWork{}) {
		t.Fatalf("drain released the Session with live work: %+v", released)
	}
	continued := logged("central sampler drain keeps serving Session with live work")
	if continued["session_id"] != f.session.ID || continued["live_cells"] != int64(1) {
		t.Fatalf("drain continuation record=%v", continued)
	}
	successor, err := f.store.Claim(t.Context(), f.session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	assertNoUnresolvedNestedIntents(t, f.store, successor)
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
	cancel, done := startRuntimeForTest(t, f.runtime)
	defer cancel()
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
	assertNoUnresolvedNestedIntents(t, f.store, successor)
	work, err := f.store.LiveWork(t.Context(), successor)
	if err != nil || work != (LiveWork{}) {
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
