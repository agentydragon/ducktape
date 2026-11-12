package codemode

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

const maxCodeModeFrame = 64 * 1024 * 1024

type Session struct {
	manager        *Manager
	host           *Process
	journalSink    Journal
	handleDelegate DelegateHandler
	sessionID      string
	mu             sync.Mutex
	delegates      map[int64]context.CancelFunc
	delegateCtx    map[int64]context.Context
	delegateWG     sync.WaitGroup
	error          error
	detached       bool
	done           chan struct{}
	bindingCtx     context.Context
	cancel         context.CancelFunc
	detachOnce     sync.Once
	delegateDone   chan struct{}
	journal        chan codeJournalEvent
	journalDone    chan struct{}
	// foreground is the execute or wait request whose observation the model is waiting on.
	foreground *int64
	yielded    bool
}

type codeJournalEvent struct {
	operationID string
	object      map[string]any
	done        chan error
}

type ResponsePhase string

const (
	OperationResponsePhase ResponsePhase = "operation_response"
	InitialResponsePhase   ResponsePhase = "execute_initial_response"
)

type ResponseError struct {
	Method  string
	Phase   ResponsePhase
	Message string
}

func (e *ResponseError) Error() string {
	return fmt.Sprintf("code-mode operation failed: %s", e.Message)
}

func (e *ResponseError) Evidence() map[string]any {
	evidence := map[string]any{
		"status":  "error",
		"method":  e.Method,
		"phase":   string(e.Phase),
		"message": e.Message,
	}
	if e.Method == "session/execute" && e.Phase == OperationResponsePhase {
		evidence["execution_started"] = false
	}
	return evidence
}

func newSession(parent context.Context, manager *Manager, host *Process, journal Journal, delegate DelegateHandler, sessionID string) *Session {
	bindingCtx, cancel := context.WithCancel(parent)
	runtime := &Session{
		manager: manager, host: host, journalSink: journal, handleDelegate: delegate, sessionID: sessionID,
		delegates: map[int64]context.CancelFunc{}, delegateCtx: map[int64]context.Context{}, done: make(chan struct{}), bindingCtx: bindingCtx, cancel: cancel,
		journal: make(chan codeJournalEvent, 256), journalDone: make(chan struct{}), delegateDone: make(chan struct{}),
	}
	go runtime.journalLoop()
	return runtime
}

func (r *Session) send(ctx context.Context, requestID int64, value any) error {
	return r.sendJournaled(ctx, requestID, value, value)
}

// sendJournaled journals journal in place of the sent frame value.
func (r *Session) sendJournaled(ctx context.Context, requestID int64, value, journal any) error {
	object, err := protocol.Object(journal)
	if err != nil {
		return err
	}
	operationID := r.operationID(requestID)
	if r.journalSink != nil {
		if err := r.journalSink.ProtocolEvent(ctx, operationID, eventpb.ExecutionProgress_PHASE_SEND, object); err != nil {
			return err
		}
	}
	return r.manager.send(r.host, ctx, value)
}

func (r *Session) operationID(id int64) string {
	if id == 0 {
		return r.sessionID + ":connection"
	}
	return fmt.Sprintf("%s:%d", r.sessionID, id)
}

func (r *Session) enqueueProtocol(raw []byte, id int64) (<-chan error, bool) {
	var object map[string]any
	if err := protocol.DecodeObject(raw, &object); err != nil {
		return nil, false
	}
	done := make(chan error, 1)
	event := codeJournalEvent{operationID: r.operationID(id), object: object, done: done}
	select {
	case r.journal <- event:
		return done, true
	default:
		return nil, false
	}
}

func (r *Session) journalLoop() {
	defer close(r.journalDone)
	for event := range r.journal {
		if r.journalSink == nil {
			event.done <- nil
			continue
		}
		ctx, cancel := context.WithTimeout(context.Background(), r.manager.rpcTimeout)
		err := r.journalSink.ProtocolEvent(ctx, event.operationID, eventpb.ExecutionProgress_PHASE_RECEIVE, event.object)
		cancel()
		event.done <- err
		if err != nil {
			r.fail(fmt.Errorf("write code-mode protocol evidence: %w", err))
		}
	}
}

func (r *Session) beginDelegate(id int64) bool {
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

func (r *Session) cancelDelegate(id int64) {
	r.mu.Lock()
	cancel := r.delegates[id]
	r.mu.Unlock()
	if cancel != nil {
		cancel()
	}
}

func (r *Session) delegate(id int64, frame *Frame, journaled <-chan error) {
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
	result, evidence, err := r.handleDelegate(ctx, r, *frame.Request)
	if err != nil {
		if ctx.Err() == nil {
			r.fail(err)
		}
		return
	}
	response := DelegateResponse{Type: "delegate/response", ID: id, Result: result}
	journal := DelegateResponse{Type: "delegate/response", ID: id, Result: evidence}
	if err := r.sendJournaled(ctx, id, response, journal); err != nil && (ctx.Err() == nil || (frame.Request.Type == "notification/send" && r.bindingCtx.Err() == nil)) {
		r.fail(err)
	}
}

func (r *Session) fail(err error) {
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

func (r *Session) Err() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.error
}

func (r *Session) HasDelegates() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.delegates) > 0
}

func (r *Session) Request(ctx context.Context, request Operation, execute bool) (json.RawMessage, error) {
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
	if request.method() == "session/wait" {
		defer r.setForeground(id)()
	}
	resultFrame, err := waitFrame(ctx, response)
	if err != nil {
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			cancelCtx, cancel := context.WithTimeout(context.Background(), r.manager.rpcTimeout)
			_ = r.send(cancelCtx, id, codeRequestSignal{Type: "operation/cancel", ID: id})
			cancel()
		}
		return nil, err
	}
	value, err := validateCodeModeResult(resultFrame, request.method(), OperationResponsePhase)
	if err != nil {
		return nil, err
	}
	if initial == nil {
		return value, nil
	}
	var started HostResponse
	if err := protocol.DecodeObject(value, &started); err != nil {
		return nil, err
	}
	cellID := started.CellID
	if started.Type != "execution/started" || cellID == "" {
		return nil, errors.New("code-mode execute response has no cellId")
	}
	if err := r.journalSink.CellStarted(ctx, r.CellHandle(cellID)); err != nil {
		return nil, err
	}
	defer r.setForeground(id)()
	initialFrame, err := waitFrame(ctx, initial)
	if err != nil {
		if errors.Is(err, context.Canceled) || errors.Is(err, context.DeadlineExceeded) {
			cancelCtx, cancel := context.WithTimeout(context.Background(), r.manager.rpcTimeout)
			_ = r.send(cancelCtx, id, codeRequestSignal{Type: "operation/cancel", ID: id})
			cancel()
		}
		return nil, err
	}
	return validateCodeModeResult(initialFrame, request.method(), InitialResponsePhase)
}

// setForeground marks request id as the observation the model waits on and returns its reset.
func (r *Session) setForeground(id int64) func() {
	r.mu.Lock()
	r.foreground, r.yielded = &id, false
	r.mu.Unlock()
	return func() {
		r.mu.Lock()
		r.foreground, r.yielded = nil, false
		r.mu.Unlock()
	}
}

// HasForeground reports whether an execute or wait observation is in flight and not yet yielded.
// Without an acknowledged yield-observation capability there is nothing to yield: input then waits
// for the observation to return.
func (r *Session) HasForeground() bool {
	if !r.host.acknowledged(YieldObservationCapability) {
		return false
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.foreground != nil && !r.yielded
}

// YieldForeground asks the host to return the waiting execute or wait observation now while the cell
// keeps running, at most once per request. It reports whether a yield was sent.
//
// Codex rust-v0.162.1 core/src/tools/code_mode (InstantInterrupt, default on): new user input yields
// foreground code-mode observations so the model can read it at the next sampling step.
func (r *Session) YieldForeground(ctx context.Context) (bool, error) {
	if !r.host.acknowledged(YieldObservationCapability) {
		return false, nil
	}
	r.mu.Lock()
	if r.foreground == nil || r.yielded {
		r.mu.Unlock()
		return false, nil
	}
	id := *r.foreground
	r.yielded = true
	r.mu.Unlock()
	return true, r.send(ctx, id, codeRequestSignal{Type: "operation/yield", ID: id})
}

func waitFrame(ctx context.Context, channel <-chan codeReply) (*WireResult, error) {
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

func validateCodeModeResult(frame *WireResult, method string, phase ResponsePhase) (json.RawMessage, error) {
	switch frame.Status {
	case "error":
		if frame.Message == "" {
			return nil, errors.New("code-mode error response has no message")
		}
		return nil, &ResponseError{Method: method, Phase: phase, Message: frame.Message}
	case "ok":
		var object struct{}
		if err := protocol.DecodeObject(frame.Value, &object); err != nil {
			return nil, err
		}
		return frame.Value, nil
	default:
		return nil, fmt.Errorf("unsupported code-mode response status %q", frame.Status)
	}
}

func (r *Session) Done() <-chan struct{} { return r.done }

func (r *Session) Detach(ctx context.Context) error {
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

func (r *Session) CellHandle(nativeID string) string { return r.sessionID + ":" + nativeID }

func (r *Session) NativeCell(handle string) (string, error) {
	prefix := r.sessionID + ":"
	if !strings.HasPrefix(handle, prefix) {
		return "", errors.New("cell handle belongs to another code-mode session")
	}
	return strings.TrimPrefix(handle, prefix), nil
}

func (r *Session) ID() string { return r.sessionID }
