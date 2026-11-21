package session

import (
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	runtimetesting "github.com/agentydragon/ducktape/agentplane/sampler/internal/residentruntime/testing"

	"github.com/agentydragon/ducktape/agentplane/env/jsonrpc"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/codemode"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/executionstate"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/executor"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/testsupport"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/toolcontract"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
	"github.com/agentydragon/ducktape/agentplane/sampler/testing/wirefixture"
)

func TestJSONExecPreparesNativeCodeWithoutChangingSource(t *testing.T) {
	source := "// @exec: is just a JavaScript comment in JSON code\nconst value = `quotes: \" and ${1 + 2}`;\ntext(value);"
	input := string(wirefixture.MustMarshalJSON(t, map[string]any{"code": source, "yield_time_ms": 0, "max_output_tokens": 123}))
	manager, _ := newFakeCodeModeManager(t)
	runtime := openFakeSession(t, manager, "fixture")
	prepared, err := runtime.prepareCall(context.Background(), protocol.ToolInvocation{Encoding: protocol.ExecEncodingJSON, Name: "exec", CallID: "call", Input: input})
	if err != nil {
		t.Fatal(err)
	}
	operation := prepared.operation.(codemode.ExecuteOperation)
	if operation.Request.Source != source || operation.Request.ToolCallID != "call" || operation.Request.YieldTimeMS != 0 || *operation.Request.MaxOutputTokens != 123 {
		t.Fatalf("JSON source or options changed: %+v", operation.Request)
	}
	prepared, err = runtime.prepareCall(context.Background(), protocol.ToolInvocation{Encoding: protocol.ExecEncodingJSON, Name: "exec", Input: `{"code":"text(1)"}`})
	if err != nil {
		t.Fatal(err)
	}
	if prepared.operation.(codemode.ExecuteOperation).Request.YieldTimeMS != 30_000 {
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
	runtime := &CodeMode{store: store, owner: owner}
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
	runtime := &CodeMode{store: store, owner: owner}
	output, err := runtime.Execute(t.Context(), protocol.ToolInvocation{
		Encoding: protocol.ExecEncodingJSON, Name: "exec", Input: `{"cell_id":"fixture:cell"}`,
	})
	if err == nil || output != nil || errors.As(err, new(*toolcontract.Rejection)) {
		t.Fatalf("store failure became model output: output=%+v error=%v", output, err)
	}
}

func newFakeCodeModeManager(t *testing.T, acknowledge ...string) (*CodeModeHost, func() []*runtimetesting.CodeModePeer) {
	t.Helper()
	manager, peers, _ := newFakeCodeModeManagerWithLogDirectory(t, 2*time.Second, 300*time.Second, acknowledge...)
	return manager, peers
}

func newFakeCodeModeManagerWithBudgets(t *testing.T, rpcTimeout, observationTimeout time.Duration, acknowledge ...string) (*CodeModeHost, func() []*runtimetesting.CodeModePeer) {
	t.Helper()
	manager, peers, _ := newFakeCodeModeManagerWithLogDirectory(t, rpcTimeout, observationTimeout, acknowledge...)
	return manager, peers
}

func TestRetainFailedCodeModeHostLog(t *testing.T) {
	outputs := t.TempDir()
	t.Setenv("TEST_UNDECLARED_OUTPUTS_DIR", outputs)
	directory := t.TempDir()
	const stderr = "fake child exited unexpectedly"
	if err := os.WriteFile(filepath.Join(directory, "code-mode-host.log"), []byte(stderr), 0o640); err != nil {
		t.Fatal(err)
	}
	runtimetesting.RetainFailedCodeModeHostLog(t, directory)
	name := strings.NewReplacer("/", "_", " ", "_").Replace(t.Name()) + "-" + filepath.Base(directory) + "-code-mode-host.log"
	got, err := os.ReadFile(filepath.Join(outputs, name))
	if err != nil || string(got) != stderr {
		t.Fatalf("retained log=%q err=%v", got, err)
	}
}

func TestCodeModeHostInstanceRotatesWhenHostProcessExits(t *testing.T) {
	manager, peers := newFakeCodeModeManager(t)
	before := manager.InstanceID()
	code := openFakeSession(t, manager, "host-instance-test")
	if err := code.Detach(t.Context()); err != nil {
		t.Fatal(err)
	}
	if err := peers()[0].Close(); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(3 * time.Second)
	for manager.InstanceID() == before && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if manager.InstanceID() == before {
		t.Fatal("host process exit did not rotate its durable instance identity")
	}
}

func TestCodeModeHostStampsOpenedHostWhenItFailsBeforeStamp(t *testing.T) {
	store, session, owner := runtimetesting.ClaimedSession(t, integrationDatabase)
	manager, peers := newFakeCodeModeManager(t)
	atStamp := make(chan struct{})
	allowStamp := make(chan struct{})
	journal := blockingStampJournal{codeJournal: codeJournal{store: store, owner: owner}, atStamp: atStamp, allowStamp: allowStamp}
	t.Cleanup(func() {
		select {
		case <-allowStamp:
		default:
			close(allowStamp)
		}
		ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
		defer cancel()
		_ = manager.Close(ctx)
	})
	openedID := manager.InstanceID()
	opened := make(chan error, 1)
	go func() {
		_, err := manager.Manager.OpenSession(t.Context(), session.ConversationID, journal, (&CodeMode{}).delegate)
		opened <- err
	}()
	select {
	case <-atStamp:
	case <-time.After(3 * time.Second):
		t.Fatal("host did not reach the post-ready stamp")
	}
	peer := peers()[0]
	// Hold the stamp after session/ready while the actual host exits.
	if err := peer.Close(); err != nil {
		t.Fatal(err)
	}
	deadline := time.Now().Add(3 * time.Second)
	for manager.InstanceID() == openedID && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if manager.InstanceID() == openedID {
		t.Fatal("host failure did not rotate its instance")
	}
	close(allowStamp)
	select {
	case err := <-opened:
		if !errors.Is(err, codemode.ErrHostInstanceChanged) {
			t.Fatalf("dead host returned a usable binding: %v", err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("host open did not finish")
	}
	var stamped string
	if err := store.DB().QueryRowContext(t.Context(), `SELECT code_host_instance::text FROM sampler_conversation WHERE id=$1`, session.ConversationID).Scan(&stamped); err != nil {
		t.Fatal(err)
	}
	if stamped != openedID || stamped == manager.InstanceID() {
		t.Fatalf("stamped host %q, opened %q, successor %q", stamped, openedID, manager.InstanceID())
	}
}

func openFakeSession(t *testing.T, manager *CodeModeHost, conversation string) *CodeMode {
	t.Helper()
	code, err := manager.OpenSession(context.Background(), conversation, sessionstore.Owner{ConversationID: conversation}, &executor.Executor{})
	if err != nil {
		t.Fatalf("open logical session %q: %v", conversation, err)
	}
	return code
}

func TestRecoveryUsesNewLogicalSessionWithoutRestartingSharedHost(t *testing.T) {
	store, session, owner := runtimetesting.ClaimedSession(t, integrationDatabase)
	manager, peers := newFakeCodeModeManager(t)
	manager.store = store
	first, err := manager.OpenSession(t.Context(), session.ConversationID, owner, &executor.Executor{})
	if err != nil {
		t.Fatal(err)
	}
	if err := store.CellResult(t.Context(), owner, first.CellHandle("old-cell"), "live"); err != nil {
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
	second, err := manager.OpenSession(t.Context(), session.ConversationID, newOwner, &executor.Executor{})
	if err != nil {
		t.Fatal(err)
	}
	if second.ID() == first.ID() {
		t.Fatal("recovery reused the detached JavaScript session")
	}
	if len(peers()) != 1 {
		t.Fatal("recovery restarted the shared host rather than replacing one logical session")
	}
	// A late request from the retired logical session cannot reach the new binding.
	peer := peers()[0]
	if err := peer.SendDelegate(9001, first.ID()); err != nil {
		t.Fatal(err)
	}
	runtimetesting.AssertDelegateResponse(t, receivePeerFrame(t, peer), 9001, "error")
	if second.Err() != nil {
		t.Fatalf("retired session affected its replacement: %v", second.Err())
	}
}

func receivePeerFrame(t *testing.T, peer *runtimetesting.CodeModePeer) map[string]json.RawMessage {
	t.Helper()
	deadline := time.After(3 * time.Second)
	for {
		select {
		case frame := <-peer.Frames:
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
	if first.ID() == second.ID() {
		t.Fatal("logical sessions share a protocol session ID")
	}
	peer := peers()[0]

	if err := peer.SendDelegate(1001, first.ID()); err != nil {
		t.Fatal(err)
	}
	responseA := receivePeerFrame(t, peer)
	runtimetesting.AssertDelegateResponse(t, responseA, 1001, "ok")
	var notification codemode.DelegateResponse
	if err := jsonrpc.UnmarshalObject(runtimetesting.MarshalFields(t, responseA), &notification); err != nil {
		t.Fatal(err)
	}
	notificationJSON, err := json.Marshal(notification.Result.Value)
	if err != nil {
		t.Fatal(err)
	}
	var notificationFields map[string]json.RawMessage
	if err := jsonrpc.UnmarshalObject(notificationJSON, &notificationFields); err != nil {
		t.Fatal(err)
	}
	if _, exists := notificationFields["result"]; exists {
		t.Fatalf("notification/delivered unexpectedly has a result field: %s", notificationJSON)
	}

	if err := first.Detach(context.Background()); err != nil {
		t.Fatal(err)
	}
	if err := peer.SendDelegate(1002, first.ID()); err != nil {
		t.Fatal(err)
	}
	responseDetached := receivePeerFrame(t, peer)
	runtimetesting.AssertDelegateResponse(t, responseDetached, 1002, "error")
	if first.Err() != nil {
		t.Fatalf("detaching session A failed its binding: %v", first.Err())
	}

	if err := peer.SendDelegate(2001, second.ID()); err != nil {
		t.Fatal(err)
	}
	responseB := receivePeerFrame(t, peer)
	runtimetesting.AssertDelegateResponse(t, responseB, 2001, "ok")
	if second.Err() != nil {
		t.Fatalf("session B was affected by detaching A: %v", second.Err())
	}
}

func TestCodeModeDelegateCancelUsesGlobalIDWithoutSessionID(t *testing.T) {
	manager, peers := newFakeCodeModeManager(t)
	started := make(chan context.Context, 1)
	finished := make(chan struct{})
	code, err := manager.Manager.OpenSession(t.Context(), "conversation", nil, func(ctx context.Context, _ *codemode.Session, _ codemode.Delegate) (codemode.DelegateResult, codemode.DelegateResult, error) {
		started <- ctx
		<-ctx.Done()
		close(finished)
		return codemode.DelegateResult{}, codemode.DelegateResult{}, ctx.Err()
	})
	if err != nil {
		t.Fatal(err)
	}
	peer := peers()[0]
	if err := peer.SendDelegate(3001, code.ID()); err != nil {
		t.Fatal(err)
	}
	select {
	case <-started:
	case <-time.After(3 * time.Second):
		t.Fatal("delegate did not start")
	}
	if err := peer.Send(codemode.Frame{Type: codemode.DelegateCancel, ID: int64Pointer(3001)}); err != nil {
		t.Fatal(err)
	}
	select {
	case <-finished:
	case <-time.After(3 * time.Second):
		t.Fatal("delegate/cancel did not cancel the delegate routed by global ID")
	}
	if err := code.Detach(t.Context()); err != nil {
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
		_, err := first.Request(ctx, codemode.SessionOperation{Method: "session/wait", SessionID: first.ID()}, false)
		firstDone <- err
	}()
	firstID := receiveOperationRequest(t, peer, first.ID())
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
	case fields := <-peer.Frames:
		var cancelFrame struct {
			Type string `json:"type"`
			ID   int64  `json:"id"`
		}
		if err := jsonrpc.UnmarshalObject(runtimetesting.MarshalFields(t, fields), &cancelFrame); err != nil {
			t.Fatal(err)
		}
		if cancelFrame.Type != "operation/cancel" || cancelFrame.ID != firstID {
			t.Fatalf("cancel frame=%+v, want operation/cancel id=%d", cancelFrame, firstID)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("caller cancellation did not send operation/cancel")
	}
	if len(peers()) != 1 || first.Err() != nil || second.Err() != nil {
		t.Fatal("canceling one request damaged the shared host or a logical session")
	}

	lateValue := json.RawMessage(`{"type":"late"}`)
	late := codemode.Frame{Type: codemode.Response, ID: int64Pointer(firstID), Result: &codemode.WireResult{Status: "ok", Value: lateValue}}
	if err := peer.Send(late); err != nil {
		t.Fatal(err)
	}

	secondDone := make(chan error, 1)
	go func() {
		_, err := second.Request(context.Background(), codemode.SessionOperation{Method: "session/wait", SessionID: second.ID()}, false)
		secondDone <- err
	}()
	secondID := receiveOperationRequest(t, peer, second.ID())
	if secondID <= firstID {
		t.Fatalf("request IDs did not remain globally increasing: A=%d B=%d", firstID, secondID)
	}
	value, _ := json.Marshal(codemode.HostResponse{Type: "wait/completed", SessionID: second.ID()})
	if err := peer.Send(codemode.Frame{Type: codemode.Response, ID: int64Pointer(secondID), Result: &codemode.WireResult{Status: "ok", Value: value}}); err != nil {
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
	originalHost := runtime.codeHost.InstanceID()
	unrelatedCell := code.CellHandle("unrelated-cell")
	if err := store.CellResult(t.Context(), owner, unrelatedCell, "live"); err != nil {
		t.Fatal(err)
	}

	result, advanceErr := advanceWithFakeOperation(t, runtime, live, code, peer, "session/execute", func(id int64) {
		if err := peer.Send(codemode.Frame{
			Type: codemode.Response, ID: int64Pointer(id), Result: &codemode.WireResult{Status: "error", Message: "host admission rejected"},
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
	items := testsupport.ResponsesItems(t, conversation.Transcript)
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
	if code.Err() != nil || runtime.codeHost.InstanceID() != originalHost {
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
	if !ok || hostResponse["method"] != "session/execute" || hostResponse["phase"] != string(codemode.OperationResponsePhase) || hostResponse["execution_started"] != false {
		t.Fatalf("rejection evidence=%+v", rejectedResult["code_mode_host_response"])
	}

	nextCall := protocol.ToolInvocation{
		Encoding: protocol.ExecEncodingJSON, Name: "exec", CallID: "next-call", Input: `{"code":"text(load('retained'))"}`,
	}
	runtimetesting.AppendModelToolCall(t, store, owner, nextCall)
	result, advanceErr = advanceWithFakeOperation(t, runtime, live, code, peer, "session/execute", func(id int64) {
		started, err := json.Marshal(codemode.HostResponse{Type: "execution/started", CellID: "next-cell"})
		if err != nil {
			t.Fatal(err)
		}
		if err := peer.Send(codemode.Frame{Type: codemode.Response, ID: int64Pointer(id), Result: &codemode.WireResult{Status: "ok", Value: started}}); err != nil {
			t.Fatal(err)
		}
		observation := json.RawMessage(`{"Result":{"cell_id":"next-cell","content_items":[{"type":"input_text","text":"retained"}]}}`)
		if err := peer.Send(codemode.Frame{Type: codemode.InitialResponse, ID: int64Pointer(id), Result: &codemode.WireResult{Status: "ok", Value: observation}}); err != nil {
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
	items = testsupport.ResponsesItems(t, conversation.Transcript)
	if output := items[len(items)-1]; output.CallID != nextCall.CallID || len(output.Output) < 2 || output.Output[len(output.Output)-1].Text != "retained" {
		t.Fatalf("next exec did not run through the retained logical session: %+v", output)
	}
	assertToolResultSucceeded(t, store, owner, nextCall.CallID, true)
	if code.Err() != nil || live.code.ID() != code.ID() {
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
			cell := code.CellHandle("cell")
			if err := store.CellResult(t.Context(), owner, cell, "live"); err != nil {
				t.Fatal(err)
			}
			result, advanceErr := advanceWithFakeOperation(t, runtime, live, code, peer, test.method, func(id int64) {
				if err := peer.Send(codemode.Frame{
					Type: codemode.Response, ID: int64Pointer(id), Result: &codemode.WireResult{Status: "error", Message: "host operation returned an error"},
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
			items := testsupport.ResponsesItems(t, conversation.Transcript)
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
			if !ok || hostResponse["method"] != test.method || hostResponse["phase"] != string(codemode.OperationResponsePhase) {
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
		respond func(*testing.T, *runtimetesting.CodeModePeer, int64, sessionstore.Owner, *sessionstore.Store)
	}{
		{
			name: "transport loss",
			respond: func(t *testing.T, peer *runtimetesting.CodeModePeer, _ int64, _ sessionstore.Owner, _ *sessionstore.Store) {
				peer.Crash()
			},
		},
		{
			name: "unknown response status",
			respond: func(t *testing.T, peer *runtimetesting.CodeModePeer, id int64, _ sessionstore.Owner, _ *sessionstore.Store) {
				if err := peer.Send(codemode.Frame{Type: codemode.Response, ID: int64Pointer(id), Result: &codemode.WireResult{Status: "unexpected"}}); err != nil {
					t.Fatal(err)
				}
			},
		},
		{
			name: "empty host error message",
			respond: func(t *testing.T, peer *runtimetesting.CodeModePeer, id int64, _ sessionstore.Owner, _ *sessionstore.Store) {
				if err := peer.Send(codemode.Frame{Type: codemode.Response, ID: int64Pointer(id), Result: &codemode.WireResult{Status: "error"}}); err != nil {
					t.Fatal(err)
				}
			},
		},
		{
			name: "initial response host error after execution started",
			respond: func(t *testing.T, peer *runtimetesting.CodeModePeer, id int64, _ sessionstore.Owner, _ *sessionstore.Store) {
				started, err := json.Marshal(codemode.HostResponse{Type: "execution/started", CellID: "started-cell"})
				if err != nil {
					t.Fatal(err)
				}
				if err := peer.Send(codemode.Frame{Type: codemode.Response, ID: int64Pointer(id), Result: &codemode.WireResult{Status: "ok", Value: started}}); err != nil {
					t.Fatal(err)
				}
				if err := peer.Send(codemode.Frame{Type: codemode.InitialResponse, ID: int64Pointer(id), Result: &codemode.WireResult{Status: "error", Message: "initial observation failed"}}); err != nil {
					t.Fatal(err)
				}
			},
		},
		{
			name: "store journal failure",
			respond: func(t *testing.T, peer *runtimetesting.CodeModePeer, id int64, owner sessionstore.Owner, store *sessionstore.Store) {
				if _, err := store.DB().ExecContext(t.Context(), `UPDATE sampler_conversation SET owner=$2 WHERE id=$1`, owner.ConversationID, "00000000-0000-4000-8000-000000000001"); err != nil {
					t.Fatal(err)
				}
				if err := peer.Send(codemode.Frame{Type: codemode.Response, ID: int64Pointer(id), Result: &codemode.WireResult{Status: "error", Message: "host admission rejected"}}); err != nil {
					t.Fatal(err)
				}
			},
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			call := protocol.ToolInvocation{Encoding: protocol.ExecEncodingJSON, Name: "exec", CallID: "exec-call", Input: `{"code":"text(1)"}`}
			store, owner, runtime, live, _, peer := newCodeModeAdvanceFixture(t, call)
			session, err := store.GetSessionForConversation(t.Context(), owner)
			if err != nil {
				t.Fatal(err)
			}
			advanceDone := make(chan error, 1)
			go func() {
				_, err := runtime.advance(t.Context(), owner, live, nil, session)
				advanceDone <- err
			}()
			request := runtimetesting.ReceiveCodeOperationRequest(t, peer, live.code.ID(), "session/execute")
			test.respond(t, peer, request.ID, owner, store)
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

func assertToolResultSucceeded(t *testing.T, store *sessionstore.Store, owner sessionstore.Owner, callID string, want bool) {
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

func newCodeModeAdvanceFixture(t *testing.T, call protocol.ToolInvocation) (*sessionstore.Store, sessionstore.Owner, *Driver, *liveHost, *CodeMode, *runtimetesting.CodeModePeer) {
	t.Helper()
	store, session, owner := runtimetesting.ClaimedSession(t, integrationDatabase)
	manager, peers := newFakeCodeModeManager(t)
	manager.store = store
	code, err := manager.OpenSession(t.Context(), session.ConversationID, owner, &executor.Executor{})
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
		input.CellID = code.CellHandle("cell")
		encoded, err := json.Marshal(input)
		if err != nil {
			t.Fatal(err)
		}
		call.Input = string(encoded)
	}
	runtimetesting.AppendModelToolCall(t, store, owner, call)
	execEncoding := protocol.ExecEncodingJSON
	if call.Name == "wait" {
		execEncoding = protocol.ExecEncodingFreeform
	}
	settings := samplersettings.Settings{
		Connections: map[string]samplersettings.ConnectionConfig{
			"provider": {BaseURL: "https://provider.invalid", Auth: samplersettings.AuthConfig{Type: samplersettings.AuthTypeBearer, Credential: "fixture-credential"}},
		},
		Models: map[string]samplersettings.ModelConfig{
			"fixture-model": {
				Connection: "provider", ProviderModel: "fixture-model", API: protocol.APIStyleResponses, Responses: responses.Config{Dialect: responses.DialectLite},
				ExecEncoding: execEncoding, ReasoningEfforts: []string{""},
			},
		},
		RPCTimeoutSeconds: 2,
	}
	runtime := &Driver{store: store, settings: settings, codeHost: manager, slot: testSlot{}}
	return store, owner, runtime, &liveHost{code: code, executor: &executor.Executor{}}, code, peers()[0]
}

func advanceWithFakeOperation(
	t *testing.T,
	runtime *Driver,
	live *liveHost,
	code *CodeMode,
	peer *runtimetesting.CodeModePeer,
	wantMethod string,
	respond func(int64),
) (sessionstore.TurnResult, error) {
	t.Helper()
	done := make(chan struct {
		result sessionstore.TurnResult
		err    error
	}, 1)
	session, err := runtime.store.GetSessionForConversation(t.Context(), code.owner)
	if err != nil {
		t.Fatal(err)
	}
	go func() {
		result, err := runtime.advance(t.Context(), code.owner, live, nil, session)
		done <- struct {
			result sessionstore.TurnResult
			err    error
		}{result: result, err: err}
	}()
	request := runtimetesting.ReceiveCodeOperationRequest(t, peer, code.ID(), wantMethod)
	respond(request.ID)
	select {
	case result := <-done:
		return result.result, result.err
	case <-time.After(3 * time.Second):
		t.Fatal("advance did not return after the host response")
		return "", nil
	}
}

// runtimetesting.ReceiveCodeOperationRequest returns the next request for method; an empty sessionID matches
// any logical session, which lets a test learn the ID the host manager allocated.
func receiveOperationRequest(t *testing.T, peer *runtimetesting.CodeModePeer, sessionID string) int64 {
	t.Helper()
	return runtimetesting.ReceiveCodeOperationRequest(t, peer, sessionID, "session/wait").ID
}

func TestCodeModeHostFailureFansOutThenRestartsWithoutReplay(t *testing.T) {
	manager, peers := newFakeCodeModeManager(t)
	first := openFakeSession(t, manager, "conversation-a")
	second := openFakeSession(t, manager, "conversation-b")
	oldSessionID := first.ID()
	peer := peers()[0]

	requestDone := make(chan error, 2)
	for _, runtime := range []*CodeMode{first, second} {
		go func(runtime *CodeMode) {
			_, err := runtime.Request(context.Background(), codemode.SessionOperation{Method: "session/wait", SessionID: runtime.ID()}, false)
			requestDone <- err
		}(runtime)
	}
	seenWaits := 0
	deadline := time.After(3 * time.Second)
	for seenWaits < 2 {
		select {
		case fields := <-peer.Frames:
			var envelope struct {
				Type    string          `json:"type"`
				Request json.RawMessage `json:"request"`
			}
			if err := jsonrpc.UnmarshalObject(runtimetesting.MarshalFields(t, fields), &envelope); err != nil {
				t.Fatal(err)
			}
			if envelope.Type == "operation/request" {
				var request struct {
					Method string `json:"method"`
				}
				if err := jsonrpc.UnmarshalObject(envelope.Request, &request); err != nil {
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
	peer.Crash()
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
	if third.ID() == oldSessionID {
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
		case fields := <-secondPeer.Frames:
			var envelope struct {
				Type    string          `json:"type"`
				Request json.RawMessage `json:"request"`
			}
			if err := jsonrpc.UnmarshalObject(runtimetesting.MarshalFields(t, fields), &envelope); err != nil {
				t.Fatal(err)
			}
			if envelope.Type == "operation/request" {
				var request struct {
					Method string `json:"method"`
				}
				if err := jsonrpc.UnmarshalObject(envelope.Request, &request); err != nil {
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
		{name: "acknowledged", acknowledge: []string{codemode.YieldObservationCapability}, next: "operation/yield"},
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
				_, _ = code.Request(ctx, codemode.SessionOperation{Method: "session/wait", SessionID: code.ID()}, false)
				close(done)
			}()
			id := receiveOperationRequest(t, peer, code.ID())
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
			case fields := <-peer.Frames:
				var frame struct {
					Type string `json:"type"`
					ID   int64  `json:"id"`
				}
				if err := jsonrpc.UnmarshalObject(runtimetesting.MarshalFields(t, fields), &frame); err != nil {
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

type blockingStampJournal struct {
	codeJournal
	atStamp    chan struct{}
	allowStamp chan struct{}
}

func (j blockingStampJournal) StampHost(ctx context.Context, instanceID string) error {
	close(j.atStamp)
	<-j.allowStamp
	return j.codeJournal.StampHost(ctx, instanceID)
}

// The fake child proxies its stdio to the test peer, exercising the real process lifecycle.
func TestCodeModeObservationBudgetsBoundWedgedHostWithoutExpiringCaller(t *testing.T) {
	for _, phase := range []string{"execute_ack", "execute_observation", "wait_observation"} {
		t.Run(phase, func(t *testing.T) {
			store, session, owner := runtimetesting.ClaimedSession(t, integrationDatabase)
			manager, peers := newFakeCodeModeManagerWithBudgets(t, 2*time.Second, 20*time.Millisecond)
			manager.store = store
			code, err := manager.OpenSession(t.Context(), session.ConversationID, owner, &executor.Executor{})
			if err != nil {
				t.Fatal(err)
			}
			if phase == "wait_observation" {
				if err := store.CellResult(t.Context(), owner, code.CellHandle("cell-fixture"), executionstate.CellLive); err != nil {
					t.Fatal(err)
				}
			}
			peer := peers()[0]
			ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
			defer cancel()
			execute := phase != "wait_observation"
			var request codemode.Operation = codemode.WaitOperation{
				SessionOperation: codemode.SessionOperation{Method: "session/wait", SessionID: code.ID()},
				Request:          codemode.WaitRequest{CellID: "cell-fixture", YieldTimeMS: 9007199254740991},
			}
			method := "session/wait"
			if execute {
				method = "session/execute"
				request = codemode.ExecuteOperation{
					SessionOperation: codemode.SessionOperation{Method: method, SessionID: code.ID()},
					Request:          codemode.ExecuteRequest{ToolCallID: "call-fixture", Source: "await new Promise(() => {})", YieldTimeMS: 9007199254740991},
				}
			}
			finished := make(chan error, 1)
			go func() { _, err := code.Request(ctx, request, execute); finished <- err }()
			sent := runtimetesting.ReceiveCodeOperationRequest(t, peer, code.ID(), method)
			var wire struct {
				Request struct {
					YieldTimeMS int `json:"yield_time_ms"`
				} `json:"request"`
			}
			if err := json.Unmarshal(sent.Request, &wire); err != nil {
				t.Fatal(err)
			}
			if wire.Request.YieldTimeMS != 20 {
				t.Fatalf("forwarded yield=%d, want 20ms", wire.Request.YieldTimeMS)
			}
			if phase == "execute_observation" {
				value, err := json.Marshal(codemode.HostResponse{Type: "execution/started", CellID: "cell-fixture"})
				if err != nil {
					t.Fatal(err)
				}
				if err := peer.Send(codemode.Frame{Type: codemode.Response, ID: &sent.ID, Result: &codemode.WireResult{Status: "ok", Value: value}}); err != nil {
					t.Fatal(err)
				}
			}
			select {
			case err := <-finished:
				if !errors.Is(err, context.DeadlineExceeded) {
					t.Fatalf("request error=%v", err)
				}
				if ctx.Err() != nil {
					t.Fatalf("caller expired: %v", ctx.Err())
				}
			case <-ctx.Done():
				t.Fatal("request did not respect phase budget")
			}
			if phase != "execute_ack" {
				state, err := store.ExecutionState(t.Context(), owner)
				if err != nil {
					t.Fatal(err)
				}
				if len(state.Cells) != 1 || state.Cells[0].ID != code.CellHandle("cell-fixture") || state.Cells[0].Status != executionstate.CellLive {
					t.Fatalf("observation timeout changed live cell: %+v", state.Cells)
				}
			}
			select {
			case fields := <-peer.Frames:
				var signal struct {
					Type string `json:"type"`
					ID   int64  `json:"id"`
				}
				if err := json.Unmarshal(runtimetesting.MarshalFields(t, fields), &signal); err != nil {
					t.Fatal(err)
				}
				if signal.Type != "operation/cancel" || signal.ID != sent.ID {
					t.Fatalf("timeout signal=%+v", signal)
				}
			case <-ctx.Done():
				t.Fatal("no cancellation signal")
			}
		})
	}
}

func newFakeCodeModeManagerWithLogDirectory(t *testing.T, rpcTimeout, observationTimeout time.Duration, acknowledge ...string) (*CodeModeHost, func() []*runtimetesting.CodeModePeer, string) {
	t.Helper()
	manager, peers, directory := runtimetesting.NewCodeModeManager(t, rpcTimeout, observationTimeout, acknowledge...)
	return NewCodeModeHost(manager, nil, nil), peers, directory
}
