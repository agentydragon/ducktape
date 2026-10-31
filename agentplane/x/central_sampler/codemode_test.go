package centralsampler

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/responses"
)

type fakeCodeModePeer struct {
	host       *Host
	input      *io.PipeReader
	output     *io.PipeWriter
	writeMu    sync.Mutex
	closeOnce  sync.Once
	frames     chan map[string]json.RawMessage
	closed     chan struct{}
	closeError error
	// acknowledge lists the offered optional capabilities this host supports.
	acknowledge []string
}

func TestJSONExecPreparesNativeCodeWithoutChangingSource(t *testing.T) {
	source := "// @exec: is just a JavaScript comment in JSON code\nconst value = `quotes: \" and ${1 + 2}`;\ntext(value);"
	input := protocol.CanonicalJSON(map[string]any{"code": source, "yield_time_ms": 0, "max_output_tokens": 123})
	runtime := &CodeMode{sessionID: "fixture"}
	prepared, err := runtime.prepareCall(context.Background(), protocol.ToolInvocation{Encoding: protocol.ExecEncodingJSON, Name: "exec", CallID: "call", Input: input})
	if err != nil {
		t.Fatal(err)
	}
	operation := prepared.operation.(codeExecuteOperation)
	if operation.Request.Source != source || operation.Request.ToolCallID != "call" || operation.Request.YieldTimeMS != 0 || *operation.Request.MaxOutputTokens != 123 {
		t.Fatalf("JSON source or options changed: %+v", operation.Request)
	}
	prepared, err = runtime.prepareCall(context.Background(), protocol.ToolInvocation{Encoding: protocol.ExecEncodingJSON, Name: "exec", Input: `{"code":"text(1)"}`})
	if err != nil {
		t.Fatal(err)
	}
	if prepared.operation.(codeExecuteOperation).Request.YieldTimeMS != defaultExecYieldTimeMS {
		t.Fatal("JSON exec lost default yield time")
	}
}

func TestJSONExecRejectsMalformedArgumentsBeforeDispatch(t *testing.T) {
	runtime := &CodeMode{}
	for _, input := range []string{
		`{}`, `{"code":"text(1)","cell_id":"cell"}`, `{"code":null}`, `{"code":123}`,
		`{"code":"text(1)","unknown":true}`, `{"code":"text(1)","terminate":false}`,
		`{"code":"text(1)","yield_time_ms":-1}`, `{"code":"text(1)","max_output_tokens":9007199254740992}`,
		`{"code":""}`, `{"cell_id":123}`, `{"cell_id":"cell","terminate":null}`,
	} {
		t.Run(input, func(t *testing.T) {
			if _, err := runtime.prepareCall(context.Background(), protocol.ToolInvocation{Encoding: protocol.ExecEncodingJSON, Name: "exec", Input: input}); err == nil {
				t.Fatal("accepted invalid JSON exec arguments")
			}
		})
	}
}

func TestCodeModeInputErrorsReturnOutputWithoutAHostRequest(t *testing.T) {
	for _, call := range []protocol.ToolInvocation{
		{Encoding: protocol.ExecEncodingJSON, Name: "exec", Input: `{}`},
		{Encoding: protocol.ExecEncodingJSON, Name: "exec", Input: `{"code":"text(1)","cell_id":"cell"}`},
		{Encoding: protocol.ExecEncodingJSON, Name: "exec", Input: `{"cell_id":""}`},
		{Encoding: protocol.ExecEncodingJSON, Name: "exec", Input: `{"code":"text(1)","yield_time_ms":-1}`},
		{Encoding: protocol.ExecEncodingFreeform, Name: "exec", Input: ""},
		{Encoding: protocol.ExecEncodingFreeform, Name: "exec", Input: "// @exec: {\"unknown\":true}\ntext(1)"},
		{Encoding: protocol.ExecEncodingJSON, Name: "wait", Input: `{"cell_id":42}`},
	} {
		t.Run(string(call.Encoding)+"/"+call.Name+"/"+call.Input, func(t *testing.T) {
			// A host request would dereference the absent manager/host. Invalid input
			// must instead return ordinary output for the model's matching call.
			runtime := &CodeMode{}
			output, err := runtime.Execute(t.Context(), call)
			if err != nil || len(output) != 1 || output[0].Type != "input_text" || !strings.HasPrefix(output[0].Text, "Error: ") {
				t.Fatalf("input rejection output=%+v error=%v", output, err)
			}
		})
	}
}

func TestCodeModeInvalidCellPreservesOtherLiveWork(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	if err := store.RegisterModelProcess(t.Context(), owner, "existing-process", 77, false); err != nil {
		t.Fatal(err)
	}
	if err := store.ProcessResult(t.Context(), owner, "existing-process", nil, "running", nil, false); err != nil {
		t.Fatal(err)
	}
	if err := store.CellResult(t.Context(), owner, "fixture:live-cell", "live"); err != nil {
		t.Fatal(err)
	}
	runtime := &CodeMode{store: store, owner: owner, sessionID: "fixture"}
	output, err := runtime.Execute(t.Context(), protocol.ToolInvocation{
		Encoding: protocol.ExecEncodingJSON, Name: "exec", CallID: "bad-wait",
		Input: `{"cell_id":"executor-session-id","terminate":true}`,
	})
	if err != nil || len(output) != 1 || !strings.Contains(output[0].Text, "known live cell_id") {
		t.Fatalf("invalid cell output=%+v error=%v", output, err)
	}
	state, err := store.ExecutionState(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if len(state.Cells) != 1 || state.Cells[0].Status != "live" || len(state.Processes) != 1 || state.Processes[0].Status != "running" {
		t.Fatalf("invalid cell changed other live work: %+v", state)
	}
}

func TestCodeModeStoreFailureIsNotModelInputError(t *testing.T) {
	store, owner := claimedExecutorStore(t)
	if err := store.DB().Close(); err != nil {
		t.Fatal(err)
	}
	runtime := &CodeMode{store: store, owner: owner, sessionID: "fixture"}
	output, err := runtime.Execute(t.Context(), protocol.ToolInvocation{
		Encoding: protocol.ExecEncodingJSON, Name: "exec", Input: `{"cell_id":"fixture:cell"}`,
	})
	if err == nil || output != nil || errors.As(err, new(*toolError)) {
		t.Fatalf("store failure became model output: output=%+v error=%v", output, err)
	}
}

func newFakeCodeModePeer(acknowledge []string) *fakeCodeModePeer {
	input, hostInput := io.Pipe()
	hostOutput, output := io.Pipe()
	peer := &fakeCodeModePeer{
		input: input, output: output,
		frames: make(chan map[string]json.RawMessage, 32), closed: make(chan struct{}),
		acknowledge: acknowledge,
	}
	peer.host = &Host{stdin: hostInput, stdout: hostOutput, done: make(chan struct{})}
	go peer.serve()
	return peer
}

func (p *fakeCodeModePeer) serve() {
	payload, err := readHostFrame(p.input)
	if err != nil {
		return
	}
	var hello codeHello
	if decodeProtocolObject(payload, &hello) != nil || hello.Type != "connection/hello" {
		return
	}
	granted := []string{}
	for _, capability := range hello.OptionalCapabilities {
		if slices.Contains(p.acknowledge, capability) {
			granted = append(granted, capability)
		}
	}
	_ = p.send(codeFrame{Type: codeReady, SelectedVersion: 1, Capabilities: granted})
	for {
		payload, err = readHostFrame(p.input)
		if err != nil {
			return
		}
		var fields map[string]json.RawMessage
		if decodeProtocolObject(payload, &fields) != nil {
			return
		}
		select {
		case p.frames <- fields:
		default:
		}
		var frame struct {
			Type    string          `json:"type"`
			ID      int64           `json:"id"`
			Request json.RawMessage `json:"request"`
		}
		if decodeProtocolObject(payload, &frame) != nil || frame.Type != "operation/request" {
			continue
		}
		var request struct {
			Method    string `json:"method"`
			SessionID string `json:"sessionId"`
		}
		if decodeProtocolObject(frame.Request, &request) != nil || request.Method != "session/open" {
			continue
		}
		value, _ := json.Marshal(codeHostResponse{Type: "session/ready", SessionID: request.SessionID})
		_ = p.send(codeFrame{Type: codeResponse, ID: &frame.ID, Result: &codeWireResult{Status: "ok", Value: value}})
	}
}

func (p *fakeCodeModePeer) send(value any) error {
	payload, err := json.Marshal(value)
	if err != nil {
		return err
	}
	p.writeMu.Lock()
	defer p.writeMu.Unlock()
	return writeHostFrame(p.output, payload)
}

// sendDelegate sends a blank notification: a store-free delegate round trip.
func (p *fakeCodeModePeer) sendDelegate(id int64, sessionID string) error {
	return p.sendNotification(id, sessionID, "cell-1", " ")
}

func (p *fakeCodeModePeer) sendNotification(id int64, sessionID, cellID, text string) error {
	return p.send(codeFrame{
		Type: codeDelegateRequest, ID: &id, SessionID: sessionID,
		Request: &codeDelegate{Type: "notification/send", CallID: "exec-call", CellID: cellID, Text: text},
	})
}

func (p *fakeCodeModePeer) crash() {
	_ = p.output.Close()
}

func (p *fakeCodeModePeer) close() error {
	p.closeOnce.Do(func() {
		_ = p.input.Close()
		_ = p.output.Close()
		_ = p.host.stdin.Close()
		_ = p.host.stdout.Close()
		close(p.host.done)
		close(p.closed)
	})
	return p.closeError
}

func newFakeCodeModeManager(t *testing.T, acknowledge ...string) (*CodeModeHost, func() []*fakeCodeModePeer) {
	t.Helper()
	manager := NewCodeModeHost(context.Background(), Settings{CodeModeHost: "fake", Logs: t.TempDir(), RPCTimeoutSeconds: 2}, nil)
	var mu sync.Mutex
	var peers []*fakeCodeModePeer
	manager.startProcess = func(context.Context, string, string) (*Host, error) {
		peer := newFakeCodeModePeer(acknowledge)
		mu.Lock()
		peers = append(peers, peer)
		mu.Unlock()
		return peer.host, nil
	}
	manager.closeProcess = func(host *Host) error {
		mu.Lock()
		defer mu.Unlock()
		for _, peer := range peers {
			if peer.host == host {
				return peer.close()
			}
		}
		return errors.New("fake host has no peer")
	}
	t.Cleanup(func() {
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = manager.Close(ctx)
	})
	return manager, func() []*fakeCodeModePeer {
		mu.Lock()
		defer mu.Unlock()
		return append([]*fakeCodeModePeer(nil), peers...)
	}
}

func openFakeSession(t *testing.T, manager *CodeModeHost, conversation string) *CodeMode {
	t.Helper()
	code, err := manager.OpenSession(context.Background(), conversation, Owner{ConversationID: conversation}, &Executor{})
	if err != nil {
		t.Fatalf("open logical session %q: %v", conversation, err)
	}
	return code
}

func TestRecoveryUsesNewLogicalSessionWithoutRestartingSharedHost(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	manager, peers := newFakeCodeModeManager(t)
	manager.store = store
	first, err := manager.OpenSession(t.Context(), session.ConversationID, owner, &Executor{})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.CellResult(t.Context(), owner, first.cellHandle("old-cell"), "live"); err != nil {
		t.Fatal(err)
	}
	if err := first.Detach(t.Context()); err != nil {
		t.Fatal(err)
	}
	if err := store.FailExecution(t.Context(), owner, session.ID, nil, errors.New("executor reply lost")); err != nil {
		t.Fatal(err)
	}
	if err := store.Release(t.Context(), owner); err != nil {
		t.Fatal(err)
	}
	blocked, err := store.GetSession(t.Context(), session.ID, "fixture-account")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := store.RecoverSession(t.Context(), session.ID, "fixture-account", blocked.Cursor, "abandon the detached runtime", ""); err != nil {
		t.Fatal(err)
	}
	newOwner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	second, err := manager.OpenSession(t.Context(), session.ConversationID, newOwner, &Executor{})
	if err != nil {
		t.Fatal(err)
	}
	if second.sessionID == first.sessionID {
		t.Fatal("recovery reused the detached JavaScript session")
	}
	if second.host != first.host || len(peers()) != 1 {
		t.Fatal("recovery restarted the shared host rather than replacing one logical session")
	}
	// A late request from the retired logical session cannot reach the new binding.
	peer := peers()[0]
	if err := peer.sendDelegate(9001, first.sessionID); err != nil {
		t.Fatal(err)
	}
	assertDelegateResponse(t, receivePeerFrame(t, peer), 9001, "error")
	if second.Err() != nil {
		t.Fatalf("retired session affected its replacement: %v", second.Err())
	}
}

func receivePeerFrame(t *testing.T, peer *fakeCodeModePeer) map[string]json.RawMessage {
	t.Helper()
	deadline := time.After(3 * time.Second)
	for {
		select {
		case frame := <-peer.frames:
			var frameType string
			if err := json.Unmarshal(frame["type"], &frameType); err == nil && frameType == "delegate/response" {
				return frame
			}
		case <-deadline:
			t.Fatal("timed out waiting for code-mode delegate response")
			return nil
		}
	}
}

func TestCodeModeSharesHostAndIsolatesLogicalSessionDetach(t *testing.T) {
	manager, peers := newFakeCodeModeManager(t)
	first := openFakeSession(t, manager, "conversation-a")
	second := openFakeSession(t, manager, "conversation-b")
	if got := len(peers()); got != 1 {
		t.Fatalf("host starts=%d, want one shared host", got)
	}
	if first.host != second.host {
		t.Fatal("logical sessions do not share the same host process")
	}
	if first.sessionID == second.sessionID {
		t.Fatal("logical sessions share a protocol session ID")
	}
	peer := peers()[0]

	if err := peer.sendDelegate(1001, first.sessionID); err != nil {
		t.Fatal(err)
	}
	responseA := receivePeerFrame(t, peer)
	assertDelegateResponse(t, responseA, 1001, "ok")
	var notification codeDelegateResponse
	if err := decodeProtocolObject(mustMarshalFields(t, responseA), &notification); err != nil {
		t.Fatal(err)
	}
	notificationJSON, err := json.Marshal(notification.Result.Value)
	if err != nil {
		t.Fatal(err)
	}
	var notificationFields map[string]json.RawMessage
	if err := decodeProtocolObject(notificationJSON, &notificationFields); err != nil {
		t.Fatal(err)
	}
	if _, exists := notificationFields["result"]; exists {
		t.Fatalf("notification/delivered unexpectedly has a result field: %s", notificationJSON)
	}

	if err := first.Detach(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := peer.sendDelegate(1002, first.sessionID); err != nil {
		t.Fatal(err)
	}
	responseDetached := receivePeerFrame(t, peer)
	assertDelegateResponse(t, responseDetached, 1002, "error")
	if first.Err() != nil {
		t.Fatalf("detaching session A failed its binding: %v", first.Err())
	}

	if err := peer.sendDelegate(2001, second.sessionID); err != nil {
		t.Fatal(err)
	}
	responseB := receivePeerFrame(t, peer)
	assertDelegateResponse(t, responseB, 2001, "ok")
	if second.Err() != nil {
		t.Fatalf("session B was affected by detaching A: %v", second.Err())
	}
}

func TestCodeModeDelegateCancelUsesGlobalIDWithoutSessionID(t *testing.T) {
	manager, _ := newFakeCodeModeManager(t)
	runtime := openFakeSession(t, manager, "conversation-a")
	const delegateID = 3001
	if !runtime.beginDelegate(delegateID) {
		t.Fatal("could not register active delegate")
	}
	ctx := runtime.delegateCtx[delegateID]
	manager.mu.Lock()
	manager.delegates[delegateID] = runtime
	manager.mu.Unlock()

	frame := codeFrame{Type: codeDelegateCancel, ID: int64Pointer(delegateID)}
	raw := []byte(`{"type":"delegate/cancel","id":3001}`)
	if err := manager.dispatch(runtime.host, &frame, raw); err != nil {
		t.Fatal(err)
	}
	select {
	case <-ctx.Done():
	case <-time.After(2 * time.Second):
		t.Fatal("delegate/cancel did not cancel the delegate routed by global ID")
	}
	runtime.delegateWG.Done()
	if err := runtime.Detach(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func int64Pointer(value int64) *int64 { return &value }

func TestCodeModeCallerCancellationKeepsSharedHostAndDropsLateResponse(t *testing.T) {
	manager, peers := newFakeCodeModeManager(t)
	first := openFakeSession(t, manager, "conversation-a")
	second := openFakeSession(t, manager, "conversation-b")
	peer := peers()[0]

	ctx, cancel := context.WithCancel(context.Background())
	firstDone := make(chan error, 1)
	go func() {
		_, err := first.request(ctx, codeSessionOperation{Method: "session/wait", SessionID: first.sessionID}, false)
		firstDone <- err
	}()
	firstID := receiveOperationRequest(t, peer, first.sessionID)
	cancel()
	select {
	case err := <-firstDone:
		if !errors.Is(err, context.Canceled) {
			t.Fatalf("canceled request error=%v, want context canceled", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("caller cancellation did not complete the request")
	}
	select {
	case fields := <-peer.frames:
		var cancelFrame struct {
			Type string `json:"type"`
			ID   int64  `json:"id"`
		}
		if err := decodeProtocolObject(mustMarshalFields(t, fields), &cancelFrame); err != nil {
			t.Fatal(err)
		}
		if cancelFrame.Type != "operation/cancel" || cancelFrame.ID != firstID {
			t.Fatalf("cancel frame=%+v, want operation/cancel id=%d", cancelFrame, firstID)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("caller cancellation did not send operation/cancel")
	}
	if first.host != second.host || first.Err() != nil || second.Err() != nil {
		t.Fatal("canceling one request damaged the shared host or a logical session")
	}

	lateValue := json.RawMessage(`{"type":"late"}`)
	late := codeFrame{Type: codeResponse, ID: int64Pointer(firstID), Result: &codeWireResult{Status: "ok", Value: lateValue}}
	lateRaw, err := json.Marshal(late)
	if err != nil {
		t.Fatal(err)
	}
	if err := manager.dispatch(first.host, &late, lateRaw); err != nil {
		t.Fatalf("late canceled response should be discarded: %v", err)
	}

	secondDone := make(chan error, 1)
	go func() {
		_, err := second.request(context.Background(), codeSessionOperation{Method: "session/wait", SessionID: second.sessionID}, false)
		secondDone <- err
	}()
	secondID := receiveOperationRequest(t, peer, second.sessionID)
	if secondID <= firstID {
		t.Fatalf("request IDs did not remain globally increasing: A=%d B=%d", firstID, secondID)
	}
	value, _ := json.Marshal(codeHostResponse{Type: "wait/completed", SessionID: second.sessionID})
	if err := peer.send(codeFrame{Type: codeResponse, ID: int64Pointer(secondID), Result: &codeWireResult{Status: "ok", Value: value}}); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-secondDone:
		if err != nil {
			t.Fatalf("session B request failed after A cancellation: %v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("session B response was not routed after A cancellation")
	}
}

func TestHostAdmissionRejectionPreservesLiveCodeModeSessionForNextExec(t *testing.T) {
	call := protocol.ToolInvocation{
		Encoding: protocol.ExecEncodingJSON, Name: "exec", CallID: "rejected-call", Input: `{"code":"text(1)"}`,
	}
	store, owner, runtime, live, code, peer := newCodeModeAdvanceFixture(t, call)
	originalHost := code.host
	unrelatedCell := code.cellHandle("unrelated-cell")
	if err := store.CellResult(t.Context(), owner, unrelatedCell, "live"); err != nil {
		t.Fatal(err)
	}

	result, advanceErr := advanceWithFakeOperation(t, runtime, live, code, peer, "session/execute", func(id int64) {
		if err := peer.send(codeFrame{
			Type: codeResponse, ID: int64Pointer(id), Result: &codeWireResult{Status: "error", Message: "host admission rejected"},
		}); err != nil {
			t.Fatal(err)
		}
	})
	if advanceErr != nil || result != "cell" {
		t.Fatalf("rejected exec advance=(%q, %v), want model-visible tool output", result, advanceErr)
	}
	conversation, err := store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	items := responsesItems(t, conversation.Archive)
	if conversation.Pending != nil || len(items) < 2 {
		t.Fatalf("rejected exec left pending work or no output: pending=%+v items=%+v", conversation.Pending, items)
	}
	if output := items[len(items)-1]; !output.IsToolOutput() || output.CallID != call.CallID || len(output.Output) != 1 || output.Output[0].Text != "host admission rejected" {
		t.Fatalf("rejected exec output=%+v", output)
	}
	state, err := store.ExecutionState(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	if len(state.Cells) != 1 || state.Cells[0].ID != unrelatedCell || state.Cells[0].Status != "live" {
		t.Fatalf("unrelated live cell changed after rejection: %+v", state.Cells)
	}
	if code.Err() != nil || code.host != originalHost {
		t.Fatalf("host binding changed after rejection: binding error=%v", code.Err())
	}
	assertToolResultSucceeded(t, store, owner, call.CallID, false)
	var rejectedResultRaw []byte
	if err := store.DB().QueryRowContext(t.Context(), `SELECT result FROM sampler_intent
		WHERE conversation_id=$1 AND request->>'kind'='cell'`, owner.ConversationID).Scan(&rejectedResultRaw); err != nil {
		t.Fatal(err)
	}
	var rejectedResult map[string]any
	if err := json.Unmarshal(rejectedResultRaw, &rejectedResult); err != nil {
		t.Fatal(err)
	}
	hostResponse, ok := rejectedResult["code_mode_host_response"].(map[string]any)
	if !ok || hostResponse["method"] != "session/execute" || hostResponse["phase"] != string(codeOperationResponsePhase) || hostResponse["execution_started"] != false {
		t.Fatalf("rejection evidence=%+v", rejectedResult["code_mode_host_response"])
	}

	nextCall := protocol.ToolInvocation{
		Encoding: protocol.ExecEncodingJSON, Name: "exec", CallID: "next-call", Input: `{"code":"text(load('retained'))"}`,
	}
	appendModelToolCall(t, store, owner, nextCall)
	result, advanceErr = advanceWithFakeOperation(t, runtime, live, code, peer, "session/execute", func(id int64) {
		started, err := json.Marshal(codeHostResponse{Type: "execution/started", CellID: "next-cell"})
		if err != nil {
			t.Fatal(err)
		}
		if err := peer.send(codeFrame{Type: codeResponse, ID: int64Pointer(id), Result: &codeWireResult{Status: "ok", Value: started}}); err != nil {
			t.Fatal(err)
		}
		observation := json.RawMessage(`{"Result":{"cell_id":"next-cell","content_items":[{"type":"input_text","text":"retained"}]}}`)
		if err := peer.send(codeFrame{Type: codeInitialResponse, ID: int64Pointer(id), Result: &codeWireResult{Status: "ok", Value: observation}}); err != nil {
			t.Fatal(err)
		}
	})
	if advanceErr != nil || result != "cell" {
		t.Fatalf("next exec advance=(%q, %v), want successful tool output", result, advanceErr)
	}
	conversation, err = store.Read(t.Context(), owner)
	if err != nil {
		t.Fatal(err)
	}
	items = responsesItems(t, conversation.Archive)
	if output := items[len(items)-1]; output.CallID != nextCall.CallID || len(output.Output) < 2 || output.Output[len(output.Output)-1].Text != "retained" {
		t.Fatalf("next exec did not run through the retained logical session: %+v", output)
	}
	assertToolResultSucceeded(t, store, owner, nextCall.CallID, true)
	if code.Err() != nil || live.code.sessionID != code.sessionID {
		t.Fatalf("next exec replaced or failed the logical Code Mode session: err=%v", code.Err())
	}
}

func TestHostWaitAndTerminateErrorsAreToolOutputsWithoutLifecycleClaims(t *testing.T) {
	for _, test := range []struct {
		name   string
		input  string
		method string
	}{
		{name: "wait", input: `{"cell_id":"cell"}`, method: "session/wait"},
		{name: "terminate", input: `{"cell_id":"cell","terminate":true}`, method: "session/terminate"},
	} {
		t.Run(test.name, func(t *testing.T) {
			call := protocol.ToolInvocation{Encoding: protocol.ExecEncodingJSON, Name: "wait", CallID: "wait-call", Input: test.input}
			store, owner, runtime, live, code, peer := newCodeModeAdvanceFixture(t, call)
			cell := code.cellHandle("cell")
			if err := store.CellResult(t.Context(), owner, cell, "live"); err != nil {
				t.Fatal(err)
			}
			result, advanceErr := advanceWithFakeOperation(t, runtime, live, code, peer, test.method, func(id int64) {
				if err := peer.send(codeFrame{
					Type: codeResponse, ID: int64Pointer(id), Result: &codeWireResult{Status: "error", Message: "host operation returned an error"},
				}); err != nil {
					t.Fatal(err)
				}
			})
			if advanceErr != nil || result != "cell" {
				t.Fatalf("%s error advance=(%q, %v), want model-visible tool output", test.name, result, advanceErr)
			}
			conversation, err := store.Read(t.Context(), owner)
			if err != nil {
				t.Fatal(err)
			}
			items := responsesItems(t, conversation.Archive)
			output := items[len(items)-1]
			if !output.IsToolOutput() || output.CallID != call.CallID || len(output.Output) != 1 || output.Output[0].Text != "host operation returned an error" {
				t.Fatalf("%s error output=%+v", test.name, output)
			}
			assertToolResultSucceeded(t, store, owner, call.CallID, false)
			state, err := store.ExecutionState(t.Context(), owner)
			if err != nil {
				t.Fatal(err)
			}
			if len(state.Cells) != 1 || state.Cells[0].ID != cell || state.Cells[0].Status != "live" {
				t.Fatalf("%s error changed cell lifecycle: %+v", test.name, state.Cells)
			}
			if code.Err() != nil {
				t.Fatalf("%s error failed its JavaScript binding: %v", test.name, code.Err())
			}
			var resultRaw []byte
			if err := store.DB().QueryRowContext(t.Context(), `SELECT result FROM sampler_intent
				WHERE conversation_id=$1 AND request->>'kind'='cell'`, owner.ConversationID).Scan(&resultRaw); err != nil {
				t.Fatal(err)
			}
			var intentResult map[string]any
			if err := json.Unmarshal(resultRaw, &intentResult); err != nil {
				t.Fatal(err)
			}
			hostResponse, ok := intentResult["code_mode_host_response"].(map[string]any)
			if !ok || hostResponse["method"] != test.method || hostResponse["phase"] != string(codeOperationResponsePhase) {
				t.Fatalf("%s response evidence=%+v", test.name, intentResult["code_mode_host_response"])
			}
			if _, claimedStarted := hostResponse["execution_started"]; claimedStarted || intentResult["status"] == "terminated" {
				t.Fatalf("%s error made an unverified lifecycle claim: %+v", test.name, intentResult)
			}
		})
	}
}

func TestCodeModeOperationalFailuresDoNotBecomeToolOutputs(t *testing.T) {
	for _, test := range []struct {
		name    string
		respond func(*testing.T, *fakeCodeModePeer, int64, Owner, *Store)
	}{
		{
			name: "transport loss",
			respond: func(t *testing.T, peer *fakeCodeModePeer, _ int64, _ Owner, _ *Store) {
				peer.crash()
			},
		},
		{
			name: "unknown response status",
			respond: func(t *testing.T, peer *fakeCodeModePeer, id int64, _ Owner, _ *Store) {
				if err := peer.send(codeFrame{Type: codeResponse, ID: int64Pointer(id), Result: &codeWireResult{Status: "unexpected"}}); err != nil {
					t.Fatal(err)
				}
			},
		},
		{
			name: "empty host error message",
			respond: func(t *testing.T, peer *fakeCodeModePeer, id int64, _ Owner, _ *Store) {
				if err := peer.send(codeFrame{Type: codeResponse, ID: int64Pointer(id), Result: &codeWireResult{Status: "error"}}); err != nil {
					t.Fatal(err)
				}
			},
		},
		{
			name: "initial response host error after execution started",
			respond: func(t *testing.T, peer *fakeCodeModePeer, id int64, _ Owner, _ *Store) {
				started, err := json.Marshal(codeHostResponse{Type: "execution/started", CellID: "started-cell"})
				if err != nil {
					t.Fatal(err)
				}
				if err := peer.send(codeFrame{Type: codeResponse, ID: int64Pointer(id), Result: &codeWireResult{Status: "ok", Value: started}}); err != nil {
					t.Fatal(err)
				}
				if err := peer.send(codeFrame{Type: codeInitialResponse, ID: int64Pointer(id), Result: &codeWireResult{Status: "error", Message: "initial observation failed"}}); err != nil {
					t.Fatal(err)
				}
			},
		},
		{
			name: "store journal failure",
			respond: func(t *testing.T, peer *fakeCodeModePeer, id int64, owner Owner, store *Store) {
				if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_conversation SET owner=$2 WHERE id=$1`, owner.ConversationID, "00000000-0000-4000-8000-000000000001"); err != nil {
					t.Fatal(err)
				}
				if err := peer.send(codeFrame{Type: codeResponse, ID: int64Pointer(id), Result: &codeWireResult{Status: "error", Message: "host admission rejected"}}); err != nil {
					t.Fatal(err)
				}
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			call := protocol.ToolInvocation{Encoding: protocol.ExecEncodingJSON, Name: "exec", CallID: "exec-call", Input: `{"code":"text(1)"}`}
			store, owner, runtime, live, _, peer := newCodeModeAdvanceFixture(t, call)
			advanceDone := make(chan error, 1)
			go func() {
				_, err := runtime.advance(t.Context(), owner, live, nil, &Session{Model: "fixture-model"})
				advanceDone <- err
			}()
			request := receiveCodeOperationRequest(t, peer, live.code.sessionID, "session/execute")
			test.respond(t, peer, request.id, owner, store)
			select {
			case err := <-advanceDone:
				if err == nil {
					t.Fatal("operational host or journal failure became a tool output")
				}
			case <-time.After(3 * time.Second):
				t.Fatal("advance did not return after the operational failure")
			}
			var pending []byte
			if err := store.DB().QueryRowContext(t.Context(), `SELECT pending FROM sampler_conversation WHERE id=$1`, owner.ConversationID).Scan(&pending); err != nil {
				t.Fatal(err)
			}
			if len(pending) == 0 || string(pending) == "null" {
				t.Fatalf("operational failure cleared the cell intent: %s", pending)
			}
		})
	}
}

func assertToolResultSucceeded(t *testing.T, store *Store, owner Owner, callID string, want bool) {
	t.Helper()
	var sessionID string
	if err := store.DB().QueryRowContext(t.Context(), `SELECT id FROM sampler_session WHERE conversation_id=$1`, owner.ConversationID).Scan(&sessionID); err != nil {
		t.Fatal(err)
	}
	entries, err := store.Events(t.Context(), sessionID, 0, 1024)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		completed := entry.GetEvent().GetItemCompleted()
		if completed == nil || completed.GetItemId() != callID {
			continue
		}
		tool, ok := completed.GetOutcome().(*eventpb.ItemCompleted_Tool)
		if !ok || tool.Tool.GetSucceeded() != want {
			t.Fatalf("tool result for %q = %+v, want succeeded=%t", callID, completed.GetOutcome(), want)
		}
		return
	}
	t.Fatalf("no completed tool result for %q", callID)
}

func newCodeModeAdvanceFixture(t *testing.T, call protocol.ToolInvocation) (*Store, Owner, *Runtime, *liveHost, *CodeMode, *fakeCodeModePeer) {
	t.Helper()
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	manager, peers := newFakeCodeModeManager(t)
	manager.store = store
	code, err := manager.OpenSession(t.Context(), session.ConversationID, owner, &Executor{})
	if err != nil {
		t.Fatal(err)
	}
	if call.Name == "wait" {
		var input struct {
			CellID    string `json:"cell_id"`
			Terminate bool   `json:"terminate"`
		}
		if err := json.Unmarshal([]byte(call.Input), &input); err != nil {
			t.Fatal(err)
		}
		input.CellID = code.cellHandle("cell")
		encoded, err := json.Marshal(input)
		if err != nil {
			t.Fatal(err)
		}
		call.Input = string(encoded)
	}
	appendModelToolCall(t, store, owner, call)
	execEncoding := protocol.ExecEncodingJSON
	if call.Name == "wait" {
		execEncoding = protocol.ExecEncodingFreeform
	}
	settings := Settings{
		Connections: map[string]ConnectionConfig{
			"provider": {BaseURL: "https://provider.invalid", Auth: AuthConfig{Type: AuthTypeBearer, Credential: "fixture-credential"}},
		},
		Models: map[string]ModelConfig{
			"fixture-model": {
				Connection: "provider", ProviderModel: "fixture-model", API: protocol.APIStyleResponses, Responses: responses.Config{Dialect: responses.DialectLite},
				ExecEncoding: execEncoding, ReasoningEfforts: []string{""},
			},
		},
		RPCTimeoutSeconds: 2,
	}
	runtime := NewRuntime(store, settings)
	return store, owner, runtime, &liveHost{code: code, executor: &Executor{}}, code, peers()[0]
}

func appendModelToolCall(t *testing.T, store *Store, owner Owner, call protocol.ToolInvocation) {
	t.Helper()
	intentID, err := store.Begin(t.Context(), owner, IntentRequest{Kind: PendingIntentSampling})
	if err != nil {
		t.Fatal(err)
	}
	itemType := responses.ItemFunctionCall
	if call.Encoding == protocol.ExecEncodingFreeform {
		itemType = responses.ItemCustomToolCall
	}
	item := responses.Item{Type: itemType, CallID: call.CallID, Name: call.Name}
	if call.Encoding == protocol.ExecEncodingJSON {
		item.Arguments = call.Input
	} else {
		item.Input = call.Input
	}
	if err := store.Finish(t.Context(), owner, intentID, map[string]any{"output": []responses.Item{item}}, nativeResponsesRecords(t, item), nil); err != nil {
		t.Fatal(err)
	}
}

func advanceWithFakeOperation(
	t *testing.T,
	runtime *Runtime,
	live *liveHost,
	code *CodeMode,
	peer *fakeCodeModePeer,
	wantMethod string,
	respond func(int64),
) (TurnResult, error) {
	t.Helper()
	done := make(chan struct {
		result TurnResult
		err    error
	}, 1)
	go func() {
		result, err := runtime.advance(t.Context(), code.owner, live, nil, &Session{Model: "fixture-model"})
		done <- struct {
			result TurnResult
			err    error
		}{result: result, err: err}
	}()
	request := receiveCodeOperationRequest(t, peer, code.sessionID, wantMethod)
	respond(request.id)
	select {
	case result := <-done:
		return result.result, result.err
	case <-time.After(3 * time.Second):
		t.Fatal("advance did not return after the host response")
		return "", nil
	}
}

type fakeCodeOperationRequest struct {
	id        int64
	method    string
	sessionID string
}

func receiveCodeOperationRequest(t *testing.T, peer *fakeCodeModePeer, sessionID, method string) fakeCodeOperationRequest {
	t.Helper()
	deadline := time.After(3 * time.Second)
	for {
		select {
		case fields := <-peer.frames:
			var envelope struct {
				Type    string          `json:"type"`
				ID      int64           `json:"id"`
				Request json.RawMessage `json:"request"`
			}
			if err := decodeProtocolObject(mustMarshalFields(t, fields), &envelope); err != nil {
				t.Fatal(err)
			}
			if envelope.Type != "operation/request" {
				continue
			}
			var request struct {
				Method    string `json:"method"`
				SessionID string `json:"sessionId"`
			}
			if err := decodeProtocolObject(envelope.Request, &request); err != nil {
				t.Fatal(err)
			}
			if request.Method == method && request.SessionID == sessionID {
				return fakeCodeOperationRequest{id: envelope.ID, method: request.Method, sessionID: request.SessionID}
			}
		case <-deadline:
			t.Fatalf("session %q did not issue %s", sessionID, method)
			return fakeCodeOperationRequest{}
		}
	}
}

func receiveOperationRequest(t *testing.T, peer *fakeCodeModePeer, sessionID string) int64 {
	t.Helper()
	return receiveCodeOperationRequest(t, peer, sessionID, "session/wait").id
}

func assertDelegateResponse(t *testing.T, fields map[string]json.RawMessage, wantID int64, wantStatus string) {
	t.Helper()
	var response codeDelegateResponse
	if err := decodeProtocolObject(mustMarshalFields(t, fields), &response); err != nil {
		t.Fatal(err)
	}
	if response.Type != "delegate/response" || response.ID != wantID || response.Result.Status != wantStatus {
		t.Fatalf("delegate response=%+v, want id=%d status=%q", response, wantID, wantStatus)
	}
}

func mustMarshalFields(t *testing.T, fields map[string]json.RawMessage) []byte {
	t.Helper()
	data, err := json.Marshal(fields)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func TestCodeModeHostFailureFansOutThenRestartsWithoutReplay(t *testing.T) {
	manager, peers := newFakeCodeModeManager(t)
	first := openFakeSession(t, manager, "conversation-a")
	second := openFakeSession(t, manager, "conversation-b")
	oldSessionID := first.sessionID
	peer := peers()[0]

	requestDone := make(chan error, 2)
	for _, runtime := range []*CodeMode{first, second} {
		go func(runtime *CodeMode) {
			_, err := runtime.request(context.Background(), codeSessionOperation{Method: "session/wait", SessionID: runtime.sessionID}, false)
			requestDone <- err
		}(runtime)
	}
	seenWaits := 0
	deadline := time.After(3 * time.Second)
	for seenWaits < 2 {
		select {
		case fields := <-peer.frames:
			var envelope struct {
				Type    string          `json:"type"`
				Request json.RawMessage `json:"request"`
			}
			if err := decodeProtocolObject(mustMarshalFields(t, fields), &envelope); err != nil {
				t.Fatal(err)
			}
			if envelope.Type == "operation/request" {
				var request struct {
					Method string `json:"method"`
				}
				if err := decodeProtocolObject(envelope.Request, &request); err != nil {
					t.Fatal(err)
				}
				if request.Method == "session/wait" {
					seenWaits++
				}
			}
		case <-deadline:
			t.Fatal("both sessions did not issue their pending requests")
		}
	}
	peer.crash()
	for i := 0; i < 2; i++ {
		select {
		case err := <-requestDone:
			if err == nil {
				t.Fatal("request succeeded after host failure")
			}
		case <-time.After(3 * time.Second):
			t.Fatal("host failure did not notify a pending session request")
		}
	}
	if first.Err() == nil || second.Err() == nil {
		t.Fatalf("host failure was not broadcast to attached sessions: A=%v B=%v", first.Err(), second.Err())
	}

	third := openFakeSession(t, manager, "conversation-a")
	if third.sessionID == oldSessionID {
		t.Fatalf("restarted host reused uncertain protocol session ID %q", oldSessionID)
	}
	if got := len(peers()); got != 2 {
		t.Fatalf("host starts after recovery=%d, want two total", got)
	}
	var reopened, replayedWait bool
	secondPeer := peers()[1]
	deadline = time.After(3 * time.Second)
	for !reopened {
		select {
		case fields := <-secondPeer.frames:
			var envelope struct {
				Type    string          `json:"type"`
				Request json.RawMessage `json:"request"`
			}
			if err := decodeProtocolObject(mustMarshalFields(t, fields), &envelope); err != nil {
				t.Fatal(err)
			}
			if envelope.Type == "operation/request" {
				var request struct {
					Method string `json:"method"`
				}
				if err := decodeProtocolObject(envelope.Request, &request); err != nil {
					t.Fatal(err)
				}
				switch request.Method {
				case "session/open":
					reopened = true
				case "session/wait":
					replayedWait = true
				}
			}
		case <-deadline:
			t.Fatal("restarted host did not open the new logical session")
		}
	}
	if replayedWait {
		t.Fatal("runtime replayed an operation whose prior outcome was uncertain")
	}
	if err := third.Detach(context.Background()); err != nil {
		t.Fatal(err)
	}
}

func TestForegroundObservationYieldsOnlyWhenTheHostAcknowledgesIt(t *testing.T) {
	for _, test := range []struct {
		name        string
		acknowledge []string
		next        string
	}{
		{name: "acknowledged", acknowledge: []string{yieldObservationCapability}, next: "operation/yield"},
		// Without the capability the input waits for the observation; the request ends uncancelled.
		{name: "not acknowledged", next: "operation/cancel"},
	} {
		t.Run(test.name, func(t *testing.T) {
			manager, peers := newFakeCodeModeManager(t, test.acknowledge...)
			code := openFakeSession(t, manager, "conversation")
			peer := peers()[0]
			ctx, cancel := context.WithCancel(context.Background())
			done := make(chan struct{})
			go func() {
				_, _ = code.request(ctx, codeSessionOperation{Method: "session/wait", SessionID: code.sessionID}, false)
				close(done)
			}()
			id := receiveOperationRequest(t, peer, code.sessionID)
			deadline := time.Now().Add(3 * time.Second)
			for test.next == "operation/yield" && !code.HasForeground() {
				if time.Now().After(deadline) {
					t.Fatal("wait request never became the foreground observation")
				}
				time.Sleep(time.Millisecond)
			}
			sent, err := code.YieldForeground(ctx)
			if err != nil || sent != (test.next == "operation/yield") {
				t.Fatalf("yield sent=%v err=%v", sent, err)
			}
			cancel()
			<-done
			select {
			case fields := <-peer.frames:
				var frame struct {
					Type string `json:"type"`
					ID   int64  `json:"id"`
				}
				if err := decodeProtocolObject(mustMarshalFields(t, fields), &frame); err != nil {
					t.Fatal(err)
				}
				if frame.Type != test.next || frame.ID != id {
					t.Fatalf("frame after the wait request=%+v, want %s id=%d", frame, test.next, id)
				}
			case <-time.After(3 * time.Second):
				t.Fatal("no frame followed the wait request")
			}
		})
	}
}
