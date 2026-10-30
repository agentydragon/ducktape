package centralsampler

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"slices"
	"sync"
	"testing"
	"time"
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
	input := canonicalJSON(map[string]any{"code": source, "yield_time_ms": 0, "max_output_tokens": 123})
	runtime := &CodeMode{sessionID: "fixture"}
	prepared, err := runtime.prepareCall(context.Background(), ResponsesToolCall{Type: ResponsesItemFunctionCall, Name: "exec", CallID: "call", Input: input})
	if err != nil {
		t.Fatal(err)
	}
	operation := prepared.operation.(codeExecuteOperation)
	if operation.Request.Source != source || operation.Request.ToolCallID != "call" || operation.Request.YieldTimeMS != 0 || *operation.Request.MaxOutputTokens != 123 {
		t.Fatalf("JSON source or options changed: %+v", operation.Request)
	}
	prepared, err = runtime.prepareCall(context.Background(), ResponsesToolCall{Type: ResponsesItemFunctionCall, Name: "exec", Input: `{"code":"text(1)"}`})
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
			if _, err := runtime.prepareCall(context.Background(), ResponsesToolCall{Type: ResponsesItemFunctionCall, Name: "exec", Input: input}); err == nil {
				t.Fatal("accepted invalid JSON exec arguments")
			}
		})
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

func receiveOperationRequest(t *testing.T, peer *fakeCodeModePeer, sessionID string) int64 {
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
			if request.Method == "session/wait" && request.SessionID == sessionID {
				return envelope.ID
			}
		case <-deadline:
			t.Fatalf("session %q did not issue its operation request", sessionID)
			return 0
		}
	}
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
