package centralsampler

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"time"

	executorpb "github.com/agentydragon/ducktape/agentplane/executor/executorpb"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
)

// The sampler works with semantic operation values. Native JSON frames are evidence only and
// are deliberately never decoded here.
type (
	executorEmpty   struct{}
	executorStarted struct {
		ProcessID string `json:"processId"`
	}
)

type executorEnvPolicy struct {
	Inherit               string            `json:"inherit"`
	IgnoreDefaultExcludes bool              `json:"ignoreDefaultExcludes"`
	Exclude               []string          `json:"exclude"`
	Set                   map[string]string `json:"set"`
	IncludeOnly           []string          `json:"includeOnly"`
}
type executorStart struct {
	ProcessID string             `json:"processId"`
	Argv      []string           `json:"argv"`
	Cwd       string             `json:"cwd"`
	EnvPolicy *executorEnvPolicy `json:"envPolicy,omitempty"`
	Env       map[string]string  `json:"env"`
	TTY       bool               `json:"tty"`
	PipeStdin bool               `json:"pipeStdin,omitempty"`
	Arg0      *string            `json:"arg0,omitempty"`
}
type executorRead struct {
	ProcessID string  `json:"processId"`
	AfterSeq  *uint64 `json:"afterSeq"`
	MaxBytes  int     `json:"maxBytes"`
	WaitMS    int     `json:"waitMs"`
}
type executorChunk struct {
	Seq    uint64 `json:"seq"`
	Stream string `json:"stream"`
	Chunk  []byte `json:"chunk"`
}
type executorReadResult struct {
	Chunks   []executorChunk `json:"chunks"`
	NextSeq  uint64          `json:"nextSeq"`
	Exited   bool            `json:"exited"`
	ExitCode *int            `json:"exitCode"`
	Closed   bool            `json:"closed"`
	Failure  *string         `json:"failure"`
}

func (r executorReadResult) output() []byte {
	var out []byte
	for _, chunk := range r.Chunks {
		out = append(out, chunk.Chunk...)
	}
	return out
}
func (r executorReadResult) uncertain() bool { return r.Failure != nil || r.Closed && !r.Exited }
func (r executorReadResult) drained() bool   { return r.Exited && r.Closed && len(r.Chunks) == 0 }

type executorWrite struct {
	ProcessID string `json:"processId"`
	Chunk     []byte `json:"chunk"`
	WriteID   string `json:"writeId"`
}
type executorWriteResult struct {
	Status string `json:"status"`
}
type executorSignal struct {
	ProcessID string `json:"processId"`
	Signal    string `json:"signal"`
}
type executorTerminate struct {
	ProcessID string `json:"processId"`
}
type executorTerminateResult struct {
	Running bool `json:"running"`
}
type executorFSGetMetadata struct {
	Path string `json:"path"`
}
type executorFSMetadata struct {
	IsDirectory bool   `json:"isDirectory"`
	IsFile      bool   `json:"isFile"`
	IsSymlink   bool   `json:"isSymlink"`
	Size        uint64 `json:"size"`
}
type executorFSOpen struct {
	HandleID string `json:"handleId"`
	Path     string `json:"path"`
	Mode     string `json:"mode"`
}
type executorFSOpened struct {
	HandleID string `json:"handleId"`
}
type executorFSReadBlock struct {
	HandleID string `json:"handleId"`
	Offset   uint64 `json:"offset"`
	Len      int    `json:"len"`
}
type executorFSBlock struct {
	Chunk []byte `json:"chunk"`
	EOF   bool   `json:"eof"`
}
type executorFSClose struct {
	HandleID string `json:"handleId"`
}

type executorSessionStream interface {
	Recv() (*executorpb.OpenSessionResponse, error)
}
type executorClient interface {
	OpenSession(context.Context, *executorpb.OpenSessionRequest, ...grpc.CallOption) (executorSessionStream, error)
	StartProcess(context.Context, *executorpb.StartProcessRequest, ...grpc.CallOption) (*executorpb.StartProcessResponse, error)
	ReadProcess(context.Context, *executorpb.ReadProcessRequest, ...grpc.CallOption) (*executorpb.ReadProcessResponse, error)
	WriteProcess(context.Context, *executorpb.WriteProcessRequest, ...grpc.CallOption) (*executorpb.WriteProcessResponse, error)
	SignalProcess(context.Context, *executorpb.SignalProcessRequest, ...grpc.CallOption) (*executorpb.SignalProcessResponse, error)
	TerminateProcess(context.Context, *executorpb.TerminateProcessRequest, ...grpc.CallOption) (*executorpb.TerminateProcessResponse, error)
	GetFileMetadata(context.Context, *executorpb.GetFileMetadataRequest, ...grpc.CallOption) (*executorpb.GetFileMetadataResponse, error)
	OpenFile(context.Context, *executorpb.OpenFileRequest, ...grpc.CallOption) (*executorpb.OpenFileResponse, error)
	ReadFile(context.Context, *executorpb.ReadFileRequest, ...grpc.CallOption) (*executorpb.ReadFileResponse, error)
	CloseFile(context.Context, *executorpb.CloseFileRequest, ...grpc.CallOption) (*executorpb.CloseFileResponse, error)
}

// typedExecutorClient keeps the sampler's narrow stream interface injectable in tests while
// using the same generated Executor client for direct Runner and Gateway targets.
type typedExecutorClient struct{ executorpb.ExecutorClient }

func (c typedExecutorClient) OpenSession(ctx context.Context, req *executorpb.OpenSessionRequest, opts ...grpc.CallOption) (executorSessionStream, error) {
	return c.ExecutorClient.OpenSession(ctx, req, opts...)
}

func (e *Executor) openSession(ctx context.Context, resume *string) error {
	handshakeCtx, cancelHandshake := context.WithTimeout(ctx, e.initializeTimeout)
	defer cancelHandshake()
	request := map[string]any{"kind": "executor", "method": "initialize", "resume_session_id": resume}
	intent, err := e.store.RecordNested(ctx, e.owner, request)
	if err != nil {
		return err
	}
	streamCtx, cancel := context.WithCancel(e.lifetime)
	stream, err := e.client.OpenSession(streamCtx, &executorpb.OpenSessionRequest{Scope: e.scope, RequestId: intent, ResumeSessionId: resume})
	if err != nil {
		cancel()
		transport := &executorTransportError{method: "initialize", phase: "open", intentID: intent, retainedSession: e.retainedSessionID(ctx), cause: err}
		if finishErr := e.finishUnknownIntent(ctx, intent, transport.evidence(transport.retainedSession)); finishErr != nil {
			return errors.Join(transport, fmt.Errorf("record OpenSession transport outcome: %w", finishErr))
		}
		return transport
	}
	first := make(chan struct {
		response *executorpb.OpenSessionResponse
		err      error
	}, 1)
	go func() {
		response, recvErr := stream.Recv()
		first <- struct {
			response *executorpb.OpenSessionResponse
			err      error
		}{response, recvErr}
	}()
	var opened *executorpb.OpenSessionResponse
	select {
	case result := <-first:
		opened, err = result.response, result.err
	case <-handshakeCtx.Done():
		cancel()
		select {
		case result := <-first:
			opened, err = result.response, result.err
		default:
			err = handshakeCtx.Err()
		}
	}
	if err != nil {
		cancel()
		transport := &executorTransportError{method: "initialize", phase: "receive", intentID: intent, retainedSession: e.retainedSessionID(ctx), cause: err}
		if finishErr := e.finishUnknownIntent(ctx, intent, transport.evidence(transport.retainedSession)); finishErr != nil {
			return errors.Join(transport, fmt.Errorf("record OpenSession receive outcome: %w", finishErr))
		}
		return transport
	}
	if opened == nil {
		cancel()
		err := errors.New("executor OpenSession returned an empty response")
		finishErr := e.finishUnknownIntent(ctx, intent, map[string]any{"outcome": "unknown", "request_id": intent, "reason": err.Error()})
		return errors.Join(err, finishErr)
	}
	frameErr := e.recordNativeFrames(ctx, intent, opened.ExecutionSessionId, "initialize", opened.NativeFrames)
	if opened.Status == nil || opened.Status.Outcome != executorpb.ExecutionStatus_OUTCOME_COMPLETED {
		cancel()
		return errors.Join(frameErr, e.finishOpenFailure(ctx, intent, opened))
	}
	if opened.ExecutionSessionId == "" || resume != nil && opened.ExecutionSessionId != *resume {
		cancel()
		mismatch := errors.New("executor resumed a different or empty execution session")
		finishErr := e.finishUnknownIntent(ctx, intent, map[string]any{"outcome": "unknown", "request_id": intent, "execution_session_id": opened.ExecutionSessionId, "reason": mismatch.Error()})
		return errors.Join(frameErr, mismatch, finishErr)
	}
	var shellName *string
	if opened.Shell != nil {
		e.shell = &execShell{Name: opened.Shell.Name, Path: opened.Shell.Path}
		shellName = &opened.Shell.Name
	}
	var environmentInfo any
	if opened.Shell != nil {
		environmentInfo = map[string]any{"shell": map[string]any{"name": opened.Shell.Name, "path": opened.Shell.Path}}
	}
	if err := e.store.SetExecutorSession(ctx, e.owner, opened.ExecutionSessionId, shellName); err != nil {
		cancel()
		return err
	}
	if err := e.store.FinishNested(ctx, e.owner, intent, map[string]any{
		"outcome": "completed", "request_id": intent, "execution_session_id": opened.ExecutionSessionId,
		"sessionId": opened.ExecutionSessionId, "environmentInfo": environmentInfo,
	}); err != nil {
		cancel()
		return errors.Join(frameErr, err)
	}
	if frameErr != nil {
		cancel()
		return fmt.Errorf("record OpenSession native evidence: %w", frameErr)
	}
	done := make(chan struct{})
	e.sessionID, e.sessionStream, e.sessionCancel, e.sessionDone = opened.ExecutionSessionId, stream, cancel, done
	e.sessionStateMu.Lock()
	e.sessionGeneration++
	generation := e.sessionGeneration
	e.reconnectRequired = false
	if resume == nil && e.lostSessionConfirmed {
		e.lostSessionConfirmed = false
	}
	e.sessionStateMu.Unlock()
	go e.watchSession(streamCtx, stream, intent, opened.ExecutionSessionId, generation, cancel, done)
	return nil
}

func (e *Executor) finishOpenFailure(ctx context.Context, intent string, response *executorpb.OpenSessionResponse) error {
	if response.Status != nil && response.Status.Outcome == executorpb.ExecutionStatus_OUTCOME_REJECTED {
		message := response.Status.Message
		if message == "" {
			message = "session open rejected"
		}
		rejectedError := &executorRejectedError{method: "initialize", code: int(response.Status.GetUpstreamCode()), message: message}
		rejected := map[string]any{"code": response.Status.UpstreamCode, "message": message}
		if err := e.store.FinishNested(ctx, e.owner, intent, map[string]any{"outcome": "rejected", "request_id": intent, "rejected": rejected}); err != nil {
			return errors.Join(rejectedError, err)
		}
		rejectedError.recorded = true
		return rejectedError
	}
	message := "execution session open outcome is unknown"
	if response.Status != nil && response.Status.Message != "" {
		message = response.Status.Message
	}
	if err := e.store.FinishNested(ctx, e.owner, intent, map[string]any{"outcome": "unknown", "request_id": intent, "message": message}); err != nil {
		return errors.Join(errors.New(message), fmt.Errorf("record unknown OpenSession outcome: %w", err))
	}
	return &executorTransportError{method: "initialize", phase: "response", intentID: intent, cause: errors.New(message)}
}

func (e *Executor) watchSession(streamCtx context.Context, stream executorSessionStream, operationID, sessionID string, generation uint64, cancel context.CancelFunc, done chan struct{}) {
	defer close(done)
	for {
		response, err := stream.Recv()
		if err != nil {
			e.sessionStateMu.Lock()
			if !e.closing && e.sessionGeneration == generation {
				e.reconnectRequired = true
			}
			e.sessionStateMu.Unlock()
			return
		}
		if err := e.recordNativeFrames(streamCtx, operationID, sessionID, "session_notification", response.NativeFrames); err != nil {
			e.sessionStateMu.Lock()
			if e.sessionGeneration == generation {
				e.sessionError = fmt.Errorf("record execution session notification evidence: %w", err)
				e.reconnectRequired = true
			}
			e.sessionStateMu.Unlock()
			cancel()
			return
		}
		if response.Status != nil {
			e.sessionStateMu.Lock()
			if !e.closing && e.sessionGeneration == generation {
				e.reconnectRequired = true
			}
			e.sessionStateMu.Unlock()
			cancel()
			return
		}
		if response.ExecutionSessionId != "" && response.ExecutionSessionId != sessionID {
			e.sessionStateMu.Lock()
			if e.sessionGeneration == generation {
				e.reconnectRequired = true
			}
			e.sessionStateMu.Unlock()
			cancel()
			return
		}
	}
}

func (e *Executor) recordNativeFrames(ctx context.Context, operationID, sessionID, method string, frames []*executorpb.ExecutionNativeFrame) error {
	if ctx.Err() != nil {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
	}
	for _, frame := range frames {
		if frame == nil {
			continue
		}
		payload := map[string]any{"request_id": operationID, "execution_session_id": sessionID, "payload": frame.Payload}
		if method == "fs/readBlock" {
			digest := sha256.Sum256(frame.Payload)
			payload["payload_digest_sha256"] = hex.EncodeToString(digest[:])
			payload["payload_bytes"] = len(frame.Payload)
			delete(payload, "payload")
		}
		if err := e.store.ProtocolEvent(ctx, e.owner, operationID, "executor", frame.Direction, payload); err != nil {
			return err
		}
	}
	return nil
}

func (e *Executor) rpc(ctx context.Context, method string, params any, resultEvidence func(map[string]any) map[string]any) (json.RawMessage, error) {
	e.rpcMu.Lock()
	defer e.rpcMu.Unlock()
	if e.rpcTimeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, e.rpcTimeout)
		defer cancel()
	}
	e.sessionStateMu.Lock()
	sessionError, reconnectRequired := e.sessionError, e.reconnectRequired
	e.sessionStateMu.Unlock()
	if sessionError != nil {
		return nil, fmt.Errorf("execution session evidence journal failed; refusing further dispatch: %w", sessionError)
	}
	if reconnectRequired {
		if err := e.reconnectLocked(ctx, executorProcessID(params)); err != nil {
			return nil, &executorNotDispatchedError{method: method, processID: executorProcessID(params), retainedSession: e.retainedSessionID(ctx), cause: err}
		}
	}
	requestJSON, err := protocolObject(map[string]any{"kind": "executor", "method": method, "params": params})
	if err != nil {
		return nil, err
	}
	intent, err := e.store.RecordNested(ctx, e.owner, requestJSON)
	if err != nil {
		return nil, err
	}
	request, err := e.typedRequest(method, params, intent)
	if err != nil {
		finishErr := e.store.FinishNested(ctx, e.owner, intent, map[string]any{"outcome": "not_dispatched", "request_id": intent, "method": method, "reason": err.Error()})
		return nil, errors.Join(err, finishErr)
	}
	response, err := e.call(ctx, method, request)
	readRetried := false
	var transport *executorTransportError
	if err != nil && errors.As(err, &transport) {
		e.setReconnectRequired(true)
		if method == "process/read" && ctx.Err() == nil {
			if reconnectErr := e.reconnectLocked(ctx, executorProcessID(params)); reconnectErr == nil {
				readRetried = true
				response, err = e.call(ctx, method, request)
			} else {
				transport.cause = errors.Join(transport.cause, fmt.Errorf("resume executor session: %w", reconnectErr))
			}
		}
	}
	if err != nil {
		if errors.As(err, &transport) {
			transport.intentID, transport.processID = intent, executorProcessID(params)
			transport.retainedSession = e.retainedSessionID(ctx)
			e.setReconnectRequired(true)
			if finishErr := e.finishUnknownIntent(ctx, intent, transport.evidence(transport.retainedSession)); finishErr != nil {
				return nil, errors.Join(transport, fmt.Errorf("record unknown typed execution %s outcome: %w", method, finishErr))
			}
		}
		return nil, err
	}
	statusValue := executionStatus(response)
	frames := executionFrames(response)
	frameErr := e.recordNativeFrames(ctx, intent, e.sessionID, method, frames)
	var frameJournalErr error
	if frameErr != nil {
		frameJournalErr = fmt.Errorf("record typed %s native evidence: %w", method, frameErr)
		e.setSessionError(frameJournalErr)
		e.setReconnectRequired(true)
	}
	if statusValue == nil {
		statusValue = &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_UNKNOWN, Message: "typed execution response omitted its status"}
	}
	if statusValue.Outcome == executorpb.ExecutionStatus_OUTCOME_UNKNOWN {
		if method == "process/read" && !readRetried && ctx.Err() == nil && frameErr == nil {
			e.setReconnectRequired(true)
			if reconnectErr := e.reconnectLocked(ctx, executorProcessID(params)); reconnectErr == nil {
				readRetried = true
				response, err = e.call(ctx, method, request)
				if err == nil {
					statusValue = executionStatus(response)
					frames = executionFrames(response)
					frameErr = errors.Join(frameErr, e.recordNativeFrames(ctx, intent, e.sessionID, method, frames))
					if frameErr != nil && frameJournalErr == nil {
						frameJournalErr = fmt.Errorf("record typed %s native evidence: %w", method, frameErr)
						e.setSessionError(frameJournalErr)
						e.setReconnectRequired(true)
					}
				} else {
					statusValue.Message = errors.Join(errors.New(statusValue.Message), err).Error()
				}
			} else if statusValue.Message == "" {
				statusValue.Message = reconnectErr.Error()
			}
		}
	}
	if statusValue == nil {
		statusValue = &executorpb.ExecutionStatus{Outcome: executorpb.ExecutionStatus_OUTCOME_UNKNOWN, Message: "typed execution response omitted its status"}
	}
	if statusValue.Outcome == executorpb.ExecutionStatus_OUTCOME_REJECTED {
		message := statusValue.Message
		if message == "" {
			message = "request rejected"
		}
		rejected := map[string]any{"code": statusValue.UpstreamCode, "message": message}
		if err := e.store.FinishNested(ctx, e.owner, intent, map[string]any{"outcome": "rejected", "request_id": intent, "execution_session_id": e.sessionID, "rejected": rejected}); err != nil {
			return nil, errors.Join(&executorRejectedError{method: method, code: int(statusValue.GetUpstreamCode()), message: message}, frameJournalErr, err)
		}
		rejection := &executorRejectedError{method: method, code: int(statusValue.GetUpstreamCode()), message: message}
		return nil, errors.Join(rejection, frameJournalErr)
	}
	if statusValue.Outcome != executorpb.ExecutionStatus_OUTCOME_COMPLETED {
		message := statusValue.Message
		if message == "" {
			message = "remote execution outcome is unknown; operation was not replayed"
		}
		transport = &executorTransportError{method: method, phase: "native", intentID: intent, processID: executorProcessID(params), retainedSession: e.sessionID, cause: errors.New(message)}
		if finishErr := e.finishUnknownIntent(ctx, intent, transport.evidence(e.sessionID)); finishErr != nil {
			return nil, errors.Join(transport, frameJournalErr, fmt.Errorf("record unknown typed execution outcome: %w", finishErr))
		}
		e.setReconnectRequired(true)
		return nil, errors.Join(transport, frameJournalErr)
	}
	result, err := responseObject(method, response)
	if err != nil {
		finishErr := e.store.FinishNested(ctx, e.owner, intent, map[string]any{"outcome": "completed", "request_id": intent, "execution_session_id": e.sessionID, "result_decode_error": err.Error()})
		return nil, errors.Join(frameJournalErr, err, finishErr)
	}
	encoded, err := json.Marshal(result)
	if err != nil {
		finishErr := e.store.FinishNested(ctx, e.owner, intent, map[string]any{"outcome": "completed", "request_id": intent, "execution_session_id": e.sessionID, "result_encoding_error": err.Error()})
		return nil, errors.Join(frameJournalErr, err, finishErr)
	}
	evidenceResult := make(map[string]any, len(result))
	for key, value := range result {
		evidenceResult[key] = value
	}
	if resultEvidence != nil {
		evidenceResult = resultEvidence(result)
	}
	evidence := evidenceResult
	evidence["outcome"] = "completed"
	evidence["request_id"] = intent
	evidence["execution_session_id"] = e.sessionID
	if err := e.store.FinishNested(ctx, e.owner, intent, evidence); err != nil {
		return nil, errors.Join(frameJournalErr, fmt.Errorf("record completed typed execution %s result: %w", method, err))
	}
	if frameJournalErr != nil {
		return nil, frameJournalErr
	}
	return encoded, nil
}

func (e *Executor) reconnectLocked(ctx context.Context, notDispatchedProcessID string) error {
	e.setReconnectRequired(true)
	conversation, err := e.store.Read(ctx, e.owner)
	if err != nil {
		return err
	}
	previousSession := e.sessionID
	if e.sessionCancel != nil {
		e.sessionCancel()
		e.sessionCancel = nil
		if e.sessionDone != nil {
			select {
			case <-e.sessionDone:
			case <-ctx.Done():
				return fmt.Errorf("wait for previous execution session stream to end: %w", ctx.Err())
			}
			e.sessionDone = nil
		}
	}
	if conversation.ExecutorSession == nil {
		if !e.lostSessionConfirmed {
			return errors.New("cannot open a fresh executor session without definitive loss of the retained session")
		}
		if err := e.openSession(ctx, nil); err != nil {
			return err
		}
		return &executorSessionReplacedError{previousSession: previousSession}
	}
	retainedSession := *conversation.ExecutorSession
	if err := e.openSession(ctx, &retainedSession); err != nil {
		var rejected *executorRejectedError
		if !errors.As(err, &rejected) || !isDefinitiveLostExecutorSession(rejected, retainedSession) {
			return err
		}
		if retireErr := e.store.RetireLostExecutorSession(ctx, e.owner, retainedSession, notDispatchedProcessID); retireErr != nil {
			return errors.Join(err, fmt.Errorf("retire lost executor session: %w", retireErr))
		}
		e.lostSessionConfirmed = true
		e.shell = nil
		if freshErr := e.openSession(ctx, nil); freshErr != nil {
			return errors.Join(err, fmt.Errorf("open fresh executor session after definitive session loss: %w", freshErr))
		}
		return &executorSessionReplacedError{previousSession: retainedSession}
	}
	return nil
}

func (e *Executor) close() error {
	e.rpcMu.Lock()
	defer e.rpcMu.Unlock()
	e.sessionStateMu.Lock()
	e.closing = true
	e.sessionStateMu.Unlock()
	var streamErr error
	if e.sessionCancel != nil {
		e.sessionCancel()
		e.sessionCancel = nil
		if e.sessionDone != nil {
			select {
			case <-e.sessionDone:
			case <-time.After(5 * time.Second):
				streamErr = errors.New("timed out waiting for execution session stream to close")
			}
			e.sessionDone = nil
		}
	}
	if e.connection != nil {
		return errors.Join(streamErr, e.connection.Close())
	}
	return streamErr
}

func (e *Executor) setReconnectRequired(value bool) {
	e.sessionStateMu.Lock()
	e.reconnectRequired = value
	e.sessionStateMu.Unlock()
}

func (e *Executor) setSessionError(err error) {
	e.sessionStateMu.Lock()
	if e.sessionError == nil {
		e.sessionError = err
	}
	e.sessionStateMu.Unlock()
}

type executorTransportError struct {
	method, phase, intentID, processID, retainedSession string
	afterSequence                                       *uint64
	output                                              *headTailBuffer
	cause                                               error
}

func (e *executorTransportError) Error() string {
	return fmt.Sprintf("typed execution %s %s transport failed (intent %s%s): remote effect outcome is unknown; mutation was not replayed: %v", e.method, e.phase, e.intentID, processDescription(e.processID), e.cause)
}
func (e *executorTransportError) Unwrap() error { return e.cause }
func (e *executorTransportError) evidence(retainedSession string) map[string]any {
	result := map[string]any{"status": "abandoned", "remote_outcome": "unknown", "automatic_replay": false, "method": e.method, "phase": e.phase, "intent_id": e.intentID, "transport_error": e.cause.Error()}
	if e.processID != "" {
		result["process_id"] = e.processID
	}
	if retainedSession != "" {
		result["execution_session_id"] = retainedSession
	}
	if e.afterSequence != nil {
		result["after_seq"] = *e.afterSequence
	}
	if e.output != nil {
		result["buffered_output"] = lossyUTF8(e.output.retained())
		result["omitted_bytes"] = e.output.omitted
	}
	return result
}

type executorNotDispatchedError struct {
	method, processID, retainedSession string
	cause                              error
}

func (e *executorNotDispatchedError) Error() string {
	return fmt.Sprintf("typed execution %s was not dispatched%s because the retained session could not be resumed: %v; previous remote outcomes remain unknown and no mutation was replayed", e.method, processDescription(e.processID), e.cause)
}
func (e *executorNotDispatchedError) Unwrap() error { return e.cause }
func (e *executorNotDispatchedError) evidence() map[string]any {
	result := map[string]any{"status": "abandoned", "remote_outcome": "not_dispatched", "automatic_replay": false, "method": e.method, "transport_error": e.cause.Error()}
	if e.processID != "" {
		result["process_id"] = e.processID
	}
	if e.retainedSession != "" {
		result["execution_session_id"] = e.retainedSession
	}
	return result
}

type executorRejectedError struct {
	method   string
	code     int
	message  string
	recorded bool
}

func (e *executorRejectedError) Error() string {
	return fmt.Sprintf("exec-server rejected %s (%d): %s", e.method, e.code, e.message)
}

type executorSessionReplacedError struct {
	previousSession string
}

func (e *executorSessionReplacedError) Error() string {
	return fmt.Sprintf("retained exec-server session %q was definitively lost; its process handles were abandoned and a fresh session was opened. The requested operation was not dispatched and must not be retried automatically", e.previousSession)
}

func isDefinitiveLostExecutorSession(rejected *executorRejectedError, expectedSession string) bool {
	if rejected == nil || !rejected.recorded || rejected.method != "initialize" || rejected.code != -32600 || expectedSession == "" {
		return false
	}
	// Codex exec-server's session registry reports an absent retained session as exactly
	// "unknown session id <id>" with JSON-RPC invalid_request (-32600). Keep this classifier
	// exact so unrelated rejections and transport outages never retire a live binding.
	return rejected.message == "unknown session id "+expectedSession
}

func processDescription(id string) string {
	if id == "" {
		return ""
	}
	return " for process " + id
}

func executorProcessID(params any) string {
	switch v := params.(type) {
	case executorStart:
		return v.ProcessID
	case executorRead:
		return v.ProcessID
	case executorWrite:
		return v.ProcessID
	case executorSignal:
		return v.ProcessID
	case executorTerminate:
		return v.ProcessID
	}
	return ""
}

func (e *Executor) retainedSessionID(ctx context.Context) string {
	if ctx.Err() != nil {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
	}
	c, err := e.store.Read(ctx, e.owner)
	if err != nil || c.ExecutorSession == nil {
		return ""
	}
	return *c.ExecutorSession
}

func (e *Executor) finishUnknownIntent(ctx context.Context, intent string, evidence map[string]any) error {
	if ctx.Err() != nil {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
	}
	return e.store.FinishNested(ctx, e.owner, intent, evidence)
}

func (e *Executor) typedRequest(method string, params any, id string) (proto.Message, error) {
	base := func() (*executorpb.ExecutionScope, string, string) { return e.scope, e.sessionID, id }
	scope, sid, rid := base()
	switch method {
	case "process/start":
		p := params.(executorStart)
		policy := p.EnvPolicy
		var env *executorpb.ExecutionEnvironmentPolicy
		if policy != nil {
			env = &executorpb.ExecutionEnvironmentPolicy{Inherit: policy.Inherit, IgnoreDefaultExcludes: policy.IgnoreDefaultExcludes, Exclude: policy.Exclude, Set: policy.Set, IncludeOnly: policy.IncludeOnly}
		}
		return &executorpb.StartProcessRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, ProcessId: p.ProcessID, Argv: p.Argv, Cwd: p.Cwd, EnvironmentPolicy: env, Environment: p.Env, Tty: p.TTY, PipeStdin: p.PipeStdin, Arg0: p.Arg0}, nil
	case "process/read":
		p := params.(executorRead)
		return &executorpb.ReadProcessRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, ProcessId: p.ProcessID, AfterSeq: p.AfterSeq, MaxBytes: uint32(p.MaxBytes), WaitMs: uint32(p.WaitMS)}, nil
	case "process/write":
		p := params.(executorWrite)
		return &executorpb.WriteProcessRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, ProcessId: p.ProcessID, Data: p.Chunk, WriteId: p.WriteID}, nil
	case "process/signal":
		p := params.(executorSignal)
		return &executorpb.SignalProcessRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, ProcessId: p.ProcessID, Signal: p.Signal}, nil
	case "process/terminate":
		p := params.(executorTerminate)
		return &executorpb.TerminateProcessRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, ProcessId: p.ProcessID}, nil
	case "fs/getMetadata":
		p := params.(executorFSGetMetadata)
		return &executorpb.GetFileMetadataRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, Path: p.Path}, nil
	case "fs/open":
		p := params.(executorFSOpen)
		return &executorpb.OpenFileRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, HandleId: p.HandleID, Path: p.Path}, nil
	case "fs/readBlock":
		p := params.(executorFSReadBlock)
		return &executorpb.ReadFileRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, HandleId: p.HandleID, Offset: p.Offset, Length: uint32(p.Len)}, nil
	case "fs/close":
		p := params.(executorFSClose)
		return &executorpb.CloseFileRequest{Scope: scope, ExecutionSessionId: sid, RequestId: rid, HandleId: p.HandleID}, nil
	default:
		return nil, fmt.Errorf("unsupported typed execution method %q", method)
	}
}

func (e *Executor) call(ctx context.Context, method string, request proto.Message) (proto.Message, error) {
	var response proto.Message
	var err error
	switch method {
	case "process/start":
		response, err = e.client.StartProcess(ctx, request.(*executorpb.StartProcessRequest))
	case "process/read":
		response, err = e.client.ReadProcess(ctx, request.(*executorpb.ReadProcessRequest))
	case "process/write":
		response, err = e.client.WriteProcess(ctx, request.(*executorpb.WriteProcessRequest))
	case "process/signal":
		response, err = e.client.SignalProcess(ctx, request.(*executorpb.SignalProcessRequest))
	case "process/terminate":
		response, err = e.client.TerminateProcess(ctx, request.(*executorpb.TerminateProcessRequest))
	case "fs/getMetadata":
		response, err = e.client.GetFileMetadata(ctx, request.(*executorpb.GetFileMetadataRequest))
	case "fs/open":
		response, err = e.client.OpenFile(ctx, request.(*executorpb.OpenFileRequest))
	case "fs/readBlock":
		response, err = e.client.ReadFile(ctx, request.(*executorpb.ReadFileRequest))
	case "fs/close":
		response, err = e.client.CloseFile(ctx, request.(*executorpb.CloseFileRequest))
	default:
		return nil, fmt.Errorf("unsupported typed execution method %q", method)
	}
	if err != nil {
		return nil, &executorTransportError{method: method, phase: "grpc", cause: err}
	}
	if response == nil {
		return nil, &executorTransportError{method: method, phase: "response", cause: errors.New("typed execution response is empty")}
	}
	return response, nil
}

func executionStatus(response proto.Message) *executorpb.ExecutionStatus {
	switch r := response.(type) {
	case *executorpb.StartProcessResponse:
		return r.Status
	case *executorpb.ReadProcessResponse:
		return r.Status
	case *executorpb.WriteProcessResponse:
		return r.Status
	case *executorpb.SignalProcessResponse:
		return r.Status
	case *executorpb.TerminateProcessResponse:
		return r.Status
	case *executorpb.GetFileMetadataResponse:
		return r.Status
	case *executorpb.OpenFileResponse:
		return r.Status
	case *executorpb.ReadFileResponse:
		return r.Status
	case *executorpb.CloseFileResponse:
		return r.Status
	}
	return nil
}

func executionFrames(response proto.Message) []*executorpb.ExecutionNativeFrame {
	switch r := response.(type) {
	case *executorpb.StartProcessResponse:
		return r.NativeFrames
	case *executorpb.ReadProcessResponse:
		return r.NativeFrames
	case *executorpb.WriteProcessResponse:
		return r.NativeFrames
	case *executorpb.SignalProcessResponse:
		return r.NativeFrames
	case *executorpb.TerminateProcessResponse:
		return r.NativeFrames
	case *executorpb.GetFileMetadataResponse:
		return r.NativeFrames
	case *executorpb.OpenFileResponse:
		return r.NativeFrames
	case *executorpb.ReadFileResponse:
		return r.NativeFrames
	case *executorpb.CloseFileResponse:
		return r.NativeFrames
	}
	return nil
}

func responseObject(method string, response proto.Message) (map[string]any, error) {
	switch r := response.(type) {
	case *executorpb.StartProcessResponse:
		return map[string]any{"processId": r.ProcessId}, nil
	case *executorpb.ReadProcessResponse:
		chunks := make([]executorChunk, 0, len(r.Chunks))
		for _, c := range r.Chunks {
			chunks = append(chunks, executorChunk{Seq: c.Seq, Stream: c.Stream, Chunk: append([]byte{}, c.Data...)})
		}
		var code *int
		if r.ExitCode != nil {
			x := int(*r.ExitCode)
			code = &x
		}
		return protocolObject(executorReadResult{Chunks: chunks, NextSeq: r.NextSeq, Exited: r.Exited, ExitCode: code, Closed: r.Closed, Failure: r.Failure})
	case *executorpb.WriteProcessResponse:
		return map[string]any{"status": r.WriteStatus}, nil
	case *executorpb.SignalProcessResponse:
		return map[string]any{}, nil
	case *executorpb.TerminateProcessResponse:
		return map[string]any{"running": r.Running}, nil
	case *executorpb.GetFileMetadataResponse:
		return protocolObject(executorFSMetadata{IsDirectory: r.IsDirectory, IsFile: r.IsFile, IsSymlink: r.IsSymlink, Size: r.Size})
	case *executorpb.OpenFileResponse:
		return map[string]any{"handleId": r.HandleId}, nil
	case *executorpb.ReadFileResponse:
		return protocolObject(executorFSBlock{Chunk: append([]byte{}, r.Data...), EOF: r.Eof})
	case *executorpb.CloseFileResponse:
		return map[string]any{}, nil
	default:
		return nil, fmt.Errorf("unsupported typed execution response for %s", method)
	}
}

func decodeProtocolObject(data []byte, value any) error {
	trimmed := bytes.TrimSpace(data)
	if len(trimmed) == 0 || trimmed[0] != '{' {
		return errors.New("protocol payload must be a JSON object")
	}
	decoder := json.NewDecoder(bytes.NewReader(trimmed))
	decoder.UseNumber()
	if err := decoder.Decode(value); err != nil {
		return err
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		return errors.New("protocol payload has trailing JSON")
	}
	return nil
}

func protocolObject(value any) (map[string]any, error) {
	data, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	var object map[string]any
	if err := decodeProtocolObject(data, &object); err != nil {
		return nil, err
	}
	return object, nil
}

func executorRPC[R any, P any](ctx context.Context, e *Executor, method string, params P) (*R, error) {
	return executorRPCWithEvidence[R](ctx, e, method, params, nil)
}

func executorRPCWithEvidence[R any, P any](ctx context.Context, e *Executor, method string, params P, resultEvidence func(map[string]any) map[string]any) (*R, error) {
	raw, err := e.rpc(ctx, method, params, resultEvidence)
	if err != nil {
		return nil, err
	}
	var result R
	if err := decodeProtocolObject(raw, &result); err != nil {
		return nil, fmt.Errorf("decode typed execution %s result: %w", method, err)
	}
	return &result, nil
}
