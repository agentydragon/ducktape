package centralsampler

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"
)

const maxCodeModeFrame = 64 * 1024 * 1024

type CodeMode struct {
	manager      *CodeModeHost
	host         *Host
	store        *Store
	owner        Owner
	executor     *Executor
	sessionID    string
	mu           sync.Mutex
	delegates    map[int64]context.CancelFunc
	delegateCtx  map[int64]context.Context
	delegateWG   sync.WaitGroup
	error        error
	detached     bool
	done         chan struct{}
	bindingCtx   context.Context
	cancel       context.CancelFunc
	detachOnce   sync.Once
	delegateDone chan struct{}
	journal      chan codeJournalEvent
	journalDone  chan struct{}
}

type codeJournalEvent struct {
	operationID string
	phase       string
	object      map[string]any
	done        chan error
}

func newCodeModeBinding(parent context.Context, manager *CodeModeHost, host *Host, store *Store, owner Owner, executor *Executor, sessionID string) *CodeMode {
	bindingCtx, cancel := context.WithCancel(parent)
	runtime := &CodeMode{
		manager: manager, host: host, store: store, owner: owner, executor: executor, sessionID: sessionID,
		delegates: map[int64]context.CancelFunc{}, delegateCtx: map[int64]context.Context{}, done: make(chan struct{}), bindingCtx: bindingCtx, cancel: cancel,
		journal: make(chan codeJournalEvent, 256), journalDone: make(chan struct{}), delegateDone: make(chan struct{}),
	}
	go runtime.journalLoop()
	return runtime
}

func (r *CodeMode) send(ctx context.Context, requestID int64, value any) error {
	object, err := protocolObject(value)
	if err != nil {
		return err
	}
	operationID := r.operationID(requestID)
	if r.store != nil {
		if err := r.store.ProtocolEvent(ctx, r.owner, operationID, "javascript", "send", object); err != nil {
			return err
		}
	}
	return r.manager.send(r.host, ctx, value)
}

func (r *CodeMode) operationID(id int64) string {
	if id == 0 {
		return r.sessionID + ":connection"
	}
	return fmt.Sprintf("%s:%d", r.sessionID, id)
}

func (r *CodeMode) enqueueProtocol(raw []byte, id int64, phase string) (<-chan error, bool) {
	var object map[string]any
	if err := decodeProtocolObject(raw, &object); err != nil {
		return nil, false
	}
	done := make(chan error, 1)
	event := codeJournalEvent{operationID: r.operationID(id), phase: phase, object: object, done: done}
	select {
	case r.journal <- event:
		return done, true
	default:
		return nil, false
	}
}

func (r *CodeMode) journalLoop() {
	defer close(r.journalDone)
	for event := range r.journal {
		if r.store == nil {
			event.done <- nil
			continue
		}
		ctx, cancel := context.WithTimeout(context.Background(), r.manager.rpcTimeout)
		err := r.store.ProtocolEvent(ctx, r.owner, event.operationID, "javascript", event.phase, event.object)
		cancel()
		event.done <- err
		if err != nil {
			r.fail(fmt.Errorf("write code-mode protocol evidence: %w", err))
		}
	}
}

func (r *CodeMode) beginDelegate(id int64) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.error != nil || r.detached {
		return false
	}
	ctx, cancel := context.WithCancel(r.bindingCtx)
	r.delegates[id] = cancel
	r.delegateCtx[id] = ctx
	r.delegateWG.Add(1)
	return true
}

func (r *CodeMode) cancelDelegate(id int64) {
	r.mu.Lock()
	cancel := r.delegates[id]
	r.mu.Unlock()
	if cancel != nil {
		cancel()
	}
}

func (r *CodeMode) delegate(id int64, frame *codeFrame, journaled <-chan error) {
	defer r.delegateWG.Done()
	r.mu.Lock()
	cancel := r.delegates[id]
	ctx := r.delegateCtx[id]
	r.mu.Unlock()
	if cancel == nil || ctx == nil {
		return
	}
	defer func() {
		r.mu.Lock()
		delete(r.delegates, id)
		delete(r.delegateCtx, id)
		cancel()
		r.mu.Unlock()
		r.manager.endDelegate(r, id)
	}()
	if err := waitJournal(ctx, journaled); err != nil {
		if ctx.Err() == nil {
			r.fail(err)
		}
		return
	}
	if frame.Request == nil {
		r.fail(errors.New("code-mode delegate request has no request"))
		return
	}
	if frame.Request.Type == "notification/send" {
		response := codeDelegateResponse{Type: "delegate/response", ID: id, Result: codeDelegateResult{Status: "ok", Value: &codeToolResult{Type: "notification/delivered"}}}
		if err := r.send(ctx, id, response); err != nil && r.bindingCtx.Err() == nil {
			r.fail(err)
		}
		return
	}
	if frame.Request.Type != "tool/invoke" {
		r.fail(fmt.Errorf("unsupported code-mode delegate type %q", frame.Request.Type))
		return
	}
	invocation := frame.Request.Invocation
	if invocation.ToolName.Name == "" || invocation.ToolName.Namespace != nil {
		r.fail(errors.New("code-mode tool invocation has no supported tool name"))
		return
	}
	requestJournal, err := protocolObject(invocation)
	if err != nil {
		r.fail(err)
		return
	}
	requestJournal["kind"] = "tool"
	if invocation.CellID != "" {
		requestJournal["cell_id"] = r.cellHandle(invocation.CellID)
	}
	intent, err := r.store.RecordNested(ctx, r.owner, requestJournal)
	if err != nil {
		if ctx.Err() == nil {
			r.fail(err)
		}
		return
	}
	result, err := r.executor.Invoke(ctx, invocation.ToolName.Name, invocation.Input)
	if err != nil {
		if ctx.Err() == nil {
			r.fail(err)
		}
		return
	}
	resultEvidence, err := protocolObject(result)
	if err != nil {
		r.fail(err)
		return
	}
	if err := r.store.FinishNested(ctx, r.owner, intent, resultEvidence); err != nil {
		if ctx.Err() == nil {
			r.fail(err)
		}
		return
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		r.fail(err)
		return
	}
	response := codeDelegateResponse{Type: "delegate/response", ID: id, Result: codeDelegateResult{Status: "ok", Value: &codeToolResult{Type: "tool/result", Result: encoded}}}
	if err := r.send(ctx, id, response); err != nil && ctx.Err() == nil {
		r.fail(err)
	}
}

func (r *CodeMode) fail(err error) {
	r.mu.Lock()
	changed := false
	if r.error == nil && !r.detached {
		r.error = err
		close(r.done)
		changed = true
		for _, cancel := range r.delegates {
			cancel()
		}
	}
	r.mu.Unlock()
	if changed {
		r.manager.failBinding(r, err)
	}
}

func (r *CodeMode) Err() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.error
}

func (r *CodeMode) hasDelegates() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.delegates) > 0
}

func (r *CodeMode) request(ctx context.Context, request codeOperation, execute bool) (json.RawMessage, error) {
	if err := r.Err(); err != nil && request.method() != "session/terminate" {
		return nil, err
	}
	id, response, initial, err := r.manager.registerRequest(r, execute)
	if err != nil {
		return nil, err
	}
	defer r.manager.removeRequest(id, r)
	if err := r.send(ctx, id, codeOperationFrame{Type: "operation/request", ID: id, Request: request}); err != nil {
		return nil, err
	}
	resultFrame, err := waitFrame(ctx, response)
	if err != nil {
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			cancelCtx, cancel := context.WithTimeout(context.Background(), r.manager.rpcTimeout)
			_ = r.send(cancelCtx, id, codeCancelRequest{Type: "operation/cancel", ID: id})
			cancel()
		}
		return nil, err
	}
	value, err := validateCodeModeResult(resultFrame)
	if err != nil {
		return nil, err
	}
	if initial == nil {
		return value, nil
	}
	var started codeHostResponse
	if err := decodeProtocolObject(value, &started); err != nil {
		return nil, err
	}
	cellID := started.CellID
	if started.Type != "execution/started" || cellID == "" {
		return nil, errors.New("code-mode execute response has no cellId")
	}
	if err := r.store.CellResult(ctx, r.owner, r.cellHandle(cellID), "live"); err != nil {
		return nil, err
	}
	initialFrame, err := waitFrame(ctx, initial)
	if err != nil {
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			cancelCtx, cancel := context.WithTimeout(context.Background(), r.manager.rpcTimeout)
			_ = r.send(cancelCtx, id, codeCancelRequest{Type: "operation/cancel", ID: id})
			cancel()
		}
		return nil, err
	}
	return validateCodeModeResult(initialFrame)
}

func waitFrame(ctx context.Context, channel <-chan codeReply) (*codeWireResult, error) {
	select {
	case <-ctx.Done():
		return nil, ctx.Err()
	case reply := <-channel:
		if reply.Err != nil {
			return nil, reply.Err
		}
		if err := waitJournal(ctx, reply.Journaled); err != nil {
			return nil, err
		}
		if reply.Result == nil {
			return nil, errors.New("code-mode frame has no result object")
		}
		return reply.Result, nil
	}
}

func waitJournal(ctx context.Context, journaled <-chan error) error {
	if journaled == nil {
		return nil
	}
	select {
	case <-ctx.Done():
		return ctx.Err()
	case err := <-journaled:
		return err
	}
}

func validateCodeModeResult(frame *codeWireResult) (json.RawMessage, error) {
	if frame.Status != "ok" {
		return nil, fmt.Errorf("code-mode operation failed: %s", frame.Message)
	}
	var object struct{}
	if err := decodeProtocolObject(frame.Value, &object); err != nil {
		return nil, err
	}
	return frame.Value, nil
}

func (r *CodeMode) Execute(ctx context.Context, call ResponsesToolCall) ([]ResponsesContentPart, error) {
	request, execute, err := r.prepareCall(ctx, call)
	if err != nil {
		return nil, &KnownFailureError{Evidence: map[string]any{"reason": err.Error(), "call": call, "dispatched": false}}
	}
	value, err := r.request(ctx, request, execute)
	if err != nil {
		return nil, err
	}
	var observation codeObservation
	if !execute {
		observation, err = decodeWaitObservation(value)
	} else {
		err = decodeProtocolObject(value, &observation)
	}
	if err != nil {
		return nil, err
	}
	return r.observe(ctx, observation)
}

func (r *CodeMode) prepareCall(ctx context.Context, call ResponsesToolCall) (codeOperation, bool, error) {
	name := call.Name
	if name == "wait" {
		input := call.Input
		var arguments waitInput
		if err := decodeToolInput([]byte(input), &arguments); err != nil {
			return nil, false, err
		}
		cellID := arguments.CellID
		if cellID == "" {
			return nil, false, errors.New("wait requires a string cell_id")
		}
		terminate := arguments.Terminate
		state, err := r.store.ExecutionState(ctx, r.owner)
		if err != nil {
			return nil, false, err
		}
		known := false
		for _, cell := range state.Cells {
			if cell.ID == cellID && cell.Status == "live" {
				known = true
				break
			}
		}
		if !known {
			return nil, false, errors.New("wait requires a known live cell_id; process session_id is not accepted")
		}
		nativeID, err := r.nativeCell(cellID)
		if err != nil {
			return nil, false, err
		}
		if terminate {
			return codeTerminateOperation{codeSessionOperation: codeSessionOperation{Method: "session/terminate", SessionID: r.sessionID}, CellID: nativeID}, false, nil
		}
		yieldMS, err := toolYieldTime(arguments.YieldTimeMS, 1000)
		if err != nil {
			return nil, false, err
		}
		return codeWaitOperation{codeSessionOperation: codeSessionOperation{Method: "session/wait", SessionID: r.sessionID}, Request: codeWaitRequest{CellID: nativeID, YieldTimeMS: yieldMS}}, false, nil
	}
	if name != "exec" {
		return nil, false, fmt.Errorf("unsupported model tool %q", name)
	}
	source := call.Input
	yieldMS := 60000
	if strings.HasPrefix(source, "// @exec:") {
		pragma, rest, found := strings.Cut(source, "\n")
		if !found {
			rest = ""
		}
		var arguments execPragma
		if err := decodeToolInput([]byte(strings.TrimSpace(strings.TrimPrefix(pragma, "// @exec:"))), &arguments); err != nil {
			return nil, false, errors.New("exec pragma must contain a JSON object")
		}
		var err error
		yieldMS, err = toolYieldTime(arguments.YieldTimeMS, 60000)
		if err != nil {
			return nil, false, err
		}
		source = rest
	}
	definitions, err := json.Marshal(toolDefinitions())
	if err != nil {
		return nil, false, err
	}
	callID := call.CallID
	return codeExecuteOperation{codeSessionOperation: codeSessionOperation{Method: "session/execute", SessionID: r.sessionID}, Request: codeExecuteRequest{ToolCallID: callID, Source: source, EnabledTools: definitions, YieldTimeMS: yieldMS, MaxOutputTokens: 10000}}, true, nil
}

func (r *CodeMode) observe(ctx context.Context, observation codeObservation) ([]ResponsesContentPart, error) {
	value := observation.Cell
	status := "completed"
	switch observation.Kind {
	case codeYielded:
		status = "live"
	case codeTerminated:
		status = "terminated"
	case codeResult:
	default:
		return nil, fmt.Errorf("unsupported cell observation %q", observation.Kind)
	}
	if err := r.store.CellResult(ctx, r.owner, r.cellHandle(value.CellID), status); err != nil {
		return nil, err
	}
	items := make([]ResponsesContentPart, 0, len(value.ContentItems)+1)
	for _, raw := range value.ContentItems {
		var item ResponsesContentPart
		if err := json.Unmarshal(raw, &item); err != nil {
			return nil, err
		}
		items = append(items, item)
	}
	if observation.Kind == codeYielded {
		items = append(items, ResponsesContentPart{Type: "input_text", Text: "Script running with cell ID " + r.cellHandle(value.CellID) + ". Use wait to continue."})
	} else if observation.Kind == codeTerminated {
		items = append(items, ResponsesContentPart{Type: "input_text", Text: "Cell terminated. Partial effects may have occurred."})
	} else if value.ErrorText != nil && *value.ErrorText != "" {
		items = append(items, ResponsesContentPart{Type: "input_text", Text: *value.ErrorText})
	}
	return items, nil
}

func (r *CodeMode) Interrupt(ctx context.Context) (map[string]any, error) {
	state, err := r.store.ExecutionState(ctx, r.owner)
	if err != nil {
		return nil, err
	}
	outcomes := map[string]any{}
	for _, cell := range state.Cells {
		if !strings.HasPrefix(cell.ID, r.sessionID+":") || cell.Status != "live" {
			continue
		}
		nativeID, err := r.nativeCell(cell.ID)
		if err != nil {
			outcomes[cell.ID] = map[string]any{"error": err.Error()}
			continue
		}
		requestCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		value, requestErr := r.request(requestCtx, codeTerminateOperation{codeSessionOperation: codeSessionOperation{Method: "session/terminate", SessionID: r.sessionID}, CellID: nativeID}, false)
		cancel()
		if requestErr != nil {
			outcomes[cell.ID] = map[string]any{"error": requestErr.Error()}
			continue
		}
		observation, err := decodeWaitObservation(value)
		if err != nil {
			outcomes[cell.ID] = map[string]any{"error": err.Error()}
			continue
		}
		if _, err := r.observe(ctx, observation); err != nil {
			outcomes[cell.ID] = map[string]any{"error": err.Error()}
		} else {
			outcomes[cell.ID] = map[string]any{"stopped": observation.Kind != codeYielded}
		}
	}
	return outcomes, nil
}

func (r *CodeMode) Done() <-chan struct{} { return r.done }

func (r *CodeMode) Detach(ctx context.Context) error {
	r.detachOnce.Do(func() {
		// Unregister first. The host reader adds delegates only while holding the
		// manager lock, so no Add can race with the Wait below.
		r.manager.detach(r)
		r.mu.Lock()
		r.detached = true
		for _, cancel := range r.delegates {
			cancel()
		}
		r.cancel()
		if r.error == nil {
			close(r.done)
		}
		r.mu.Unlock()
		close(r.journal)
		go func() {
			r.delegateWG.Wait()
			close(r.delegateDone)
		}()
	})
	select {
	case <-r.journalDone:
	case <-ctx.Done():
		return ctx.Err()
	}
	select {
	case <-r.delegateDone:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (r *CodeMode) cellHandle(nativeID string) string { return r.sessionID + ":" + nativeID }

func (r *CodeMode) nativeCell(handle string) (string, error) {
	prefix := r.sessionID + ":"
	if !strings.HasPrefix(handle, prefix) {
		return "", errors.New("cell handle belongs to another code-mode session")
	}
	return strings.TrimPrefix(handle, prefix), nil
}
