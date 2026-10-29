package centralsampler

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"
	"unicode"
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
		// Codex rust-v0.162.1 core/src/tools/code_mode/delegate.rs CoreTurnHost::notify: blank text
		// is a no-op success; otherwise acknowledge only once the output is durably queued.
		result := codeDelegateResult{Status: "ok", Value: &codeToolResult{Type: "notification/delivered"}}
		if strings.TrimSpace(frame.Request.Text) != "" {
			err := r.store.QueueNotification(ctx, r.owner, frame.Request.CallID, r.cellHandle(frame.Request.CellID), frame.Request.Text)
			if errors.Is(err, ErrPrecondition) {
				result = codeDelegateResult{Status: "error", Message: fmt.Sprintf("failed to inject exec notify message for cell %s: no active turn", frame.Request.CellID)}
			} else if err != nil {
				if ctx.Err() == nil {
					r.fail(err)
				}
				return
			}
		}
		response := codeDelegateResponse{Type: "delegate/response", ID: id, Result: result}
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
	prepared, err := r.prepareCall(ctx, call)
	if err != nil {
		return nil, &KnownFailureError{Evidence: map[string]any{"reason": err.Error(), "call": call, "dispatched": false}}
	}
	started := time.Now()
	value, err := r.request(ctx, prepared.operation, prepared.execute)
	if err != nil {
		return nil, err
	}
	var observation codeObservation
	if !prepared.execute {
		observation, err = decodeWaitObservation(value)
	} else {
		err = decodeProtocolObject(value, &observation)
	}
	if err != nil {
		return nil, err
	}
	if err := r.observe(ctx, observation); err != nil {
		return nil, err
	}
	wallTime := time.Since(started)
	if duration := observation.Cell.CodeModeHostDurationNS; duration != nil {
		wallTime = time.Duration(*duration)
	}
	return cellOutput(observation, r.cellHandle(observation.Cell.CellID), prepared.maxTokens, wallTime)
}

type preparedCall struct {
	operation codeOperation
	execute   bool
	// maxTokens is the model's output token budget; nil uses the Codex default.
	maxTokens *int
}

func (r *CodeMode) prepareCall(ctx context.Context, call ResponsesToolCall) (preparedCall, error) {
	if call.Name == "wait" && call.Type == ResponsesItemFunctionCall {
		return r.prepareWait(ctx, call.Input)
	}
	if call.Name != "exec" || call.Type != ResponsesItemCustomToolCall {
		return preparedCall{}, fmt.Errorf("unsupported model tool %q (%s)", call.Name, call.Type)
	}
	source, err := parseExecSource(call.Input)
	if err != nil {
		return preparedCall{}, err
	}
	yieldMS := defaultExecYieldTimeMS
	if source.yieldTimeMS != nil {
		yieldMS = *source.yieldTimeMS
	}
	hostDefinitions, err := hostToolDefinitions(toolDefinitions())
	if err != nil {
		return preparedCall{}, err
	}
	definitions, err := json.Marshal(hostDefinitions)
	if err != nil {
		return preparedCall{}, err
	}
	request := codeExecuteRequest{ToolCallID: call.CallID, Source: source.code, EnabledTools: definitions, YieldTimeMS: yieldMS, MaxOutputTokens: source.maxOutputTokens}
	return preparedCall{
		operation: codeExecuteOperation{codeSessionOperation: codeSessionOperation{Method: "session/execute", SessionID: r.sessionID}, Request: request},
		execute:   true, maxTokens: source.maxOutputTokens,
	}, nil
}

type waitArguments struct {
	cellID      string
	yieldTimeMS int
	// maxTokens is nil when the model omits it.
	maxTokens *int
	terminate bool
}

// parseWaitArguments applies Codex rust-v0.162.1 core/src/tools/code_mode/wait_handler.rs
// ExecWaitArgs defaults.
func parseWaitArguments(input string) (waitArguments, error) {
	var arguments waitInput
	if err := decodeToolInput([]byte(input), &arguments); err != nil {
		return waitArguments{}, err
	}
	if arguments.CellID == "" {
		return waitArguments{}, errors.New("wait requires a string cell_id")
	}
	yieldMS, err := safeIntegerArgument("yield_time_ms", arguments.YieldTimeMS)
	if err != nil {
		return waitArguments{}, err
	}
	maxTokens, err := safeIntegerArgument("max_tokens", arguments.MaxTokens)
	if err != nil {
		return waitArguments{}, err
	}
	parsed := waitArguments{cellID: arguments.CellID, yieldTimeMS: defaultWaitYieldTimeMS, maxTokens: maxTokens, terminate: arguments.Terminate}
	if yieldMS != nil {
		parsed.yieldTimeMS = *yieldMS
	}
	return parsed, nil
}

func (r *CodeMode) prepareWait(ctx context.Context, input string) (preparedCall, error) {
	arguments, err := parseWaitArguments(input)
	if err != nil {
		return preparedCall{}, err
	}
	cellID := arguments.cellID
	state, err := r.store.ExecutionState(ctx, r.owner)
	if err != nil {
		return preparedCall{}, err
	}
	known := false
	for _, cell := range state.Cells {
		if cell.ID == cellID && cell.Status == "live" {
			known = true
			break
		}
	}
	if !known {
		return preparedCall{}, errors.New("wait requires a known live cell_id; process session_id is not accepted")
	}
	nativeID, err := r.nativeCell(cellID)
	if err != nil {
		return preparedCall{}, err
	}
	session := codeSessionOperation{Method: "session/wait", SessionID: r.sessionID}
	if arguments.terminate {
		session.Method = "session/terminate"
		return preparedCall{operation: codeTerminateOperation{codeSessionOperation: session, CellID: nativeID}, maxTokens: arguments.maxTokens}, nil
	}
	request := codeWaitRequest{CellID: nativeID, YieldTimeMS: arguments.yieldTimeMS}
	return preparedCall{operation: codeWaitOperation{codeSessionOperation: session, Request: request}, maxTokens: arguments.maxTokens}, nil
}

// safeIntegerArgument bounds an unsigned tool argument to the JavaScript safe integer range, as
// Codex does, so it fits the host protocol's integers.
func safeIntegerArgument(name string, value *uint64) (*int, error) {
	if value == nil {
		return nil, nil
	}
	if *value > maxJSSafeInteger {
		return nil, fmt.Errorf("%s must be a non-negative safe integer", name)
	}
	converted := int(*value)
	return &converted, nil
}

type execSource struct {
	code            string
	yieldTimeMS     *int
	maxOutputTokens *int
}

// parseExecSource ports Codex rust-v0.162.1 code-mode-protocol/src/description.rs
// parse_exec_source, including its model-facing error text.
func parseExecSource(input string) (execSource, error) {
	const supported = "supported fields `yield_time_ms` and `max_output_tokens`"
	if strings.TrimSpace(input) == "" {
		return execSource{}, errors.New("exec expects raw JavaScript source text (non-empty). Provide JS only, optionally with first-line `// @exec: {\"yield_time_ms\": 10000, \"max_output_tokens\": 1000}`.")
	}
	firstLine, rest, _ := strings.Cut(input, "\n")
	pragma, found := strings.CutPrefix(strings.TrimLeftFunc(firstLine, unicode.IsSpace), execPragmaPrefix)
	if !found {
		return execSource{code: input}, nil
	}
	if strings.TrimSpace(rest) == "" {
		return execSource{}, errors.New("exec pragma must be followed by JavaScript source on subsequent lines")
	}
	directive := strings.TrimSpace(pragma)
	if directive == "" {
		return execSource{}, errors.New("exec pragma must be a JSON object with " + supported)
	}
	var value any
	if err := json.Unmarshal([]byte(directive), &value); err != nil {
		return execSource{}, fmt.Errorf("exec pragma must be valid JSON with %s: %w", supported, err)
	}
	fields, ok := value.(map[string]any)
	if !ok {
		return execSource{}, errors.New("exec pragma must be a JSON object with " + supported)
	}
	for key := range fields {
		if key != "yield_time_ms" && key != "max_output_tokens" {
			return execSource{}, fmt.Errorf("exec pragma only supports `yield_time_ms` and `max_output_tokens`; got `%s`", key)
		}
	}
	var arguments execPragma
	if err := json.Unmarshal([]byte(directive), &arguments); err != nil {
		return execSource{}, fmt.Errorf("exec pragma fields `yield_time_ms` and `max_output_tokens` must be non-negative safe integers: %w", err)
	}
	yieldMS, err := safeIntegerArgument("exec pragma field `yield_time_ms`", arguments.YieldTimeMS)
	if err != nil {
		return execSource{}, err
	}
	maxOutputTokens, err := safeIntegerArgument("exec pragma field `max_output_tokens`", arguments.MaxOutputTokens)
	if err != nil {
		return execSource{}, err
	}
	return execSource{code: rest, yieldTimeMS: yieldMS, maxOutputTokens: maxOutputTokens}, nil
}

// observe records the observed cell's lifecycle status.
func (r *CodeMode) observe(ctx context.Context, observation codeObservation) error {
	status := "completed"
	switch observation.Kind {
	case codeYielded:
		status = "live"
	case codeTerminated:
		status = "terminated"
	case codeResult:
	default:
		return fmt.Errorf("unsupported cell observation %q", observation.Kind)
	}
	return r.store.CellResult(ctx, r.owner, r.cellHandle(observation.Cell.CellID), status)
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
		if err := r.observe(ctx, observation); err != nil {
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
