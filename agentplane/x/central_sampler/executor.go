package centralsampler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"math/rand/v2"
	"net/url"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/grpc/bearer"
	executorpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/executor_gatewaypb"
	samplerpb "github.com/agentydragon/ducktape/agentplane/x/central_sampler/samplerpb"
	"github.com/coder/websocket"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"
)

type executorSocket interface {
	Send(context.Context, []byte) error
	Receive(context.Context) ([]byte, error)
	Close(context.Context) error
}

type gatewayExecutorSocket struct {
	stream grpc.BidiStreamingClient[executorpb.ExecutorFrame, executorpb.ExecutorFrame]
	cancel context.CancelFunc
	mu     sync.Mutex
}

func (s *gatewayExecutorSocket) Send(ctx context.Context, payload []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	completed := make(chan error, 1)
	go func() {
		completed <- s.stream.Send(&executorpb.ExecutorFrame{Frame: &executorpb.ExecutorFrame_Message{Message: &executorpb.ExecutorMessage{Opcode: executorpb.ExecutorMessage_OPCODE_TEXT, Payload: payload}}})
	}()
	select {
	case err := <-completed:
		// gRPC Send reports EOF when the peer has already closed the stream.
		// Recv carries the terminal status explaining that closure.
		if errors.Is(err, io.EOF) {
			for {
				_, terminal := s.Receive(ctx)
				if terminal != nil {
					return terminal
				}
			}
		}
		return err
	case <-ctx.Done():
		s.cancel()
		return <-completed
	}
}

func (s *gatewayExecutorSocket) Receive(ctx context.Context) ([]byte, error) {
	completed := make(chan struct {
		frame *executorpb.ExecutorFrame
		err   error
	}, 1)
	go func() {
		frame, err := s.stream.Recv()
		completed <- struct {
			frame *executorpb.ExecutorFrame
			err   error
		}{frame, err}
	}()
	select {
	case result := <-completed:
		if result.err != nil {
			return nil, result.err
		}
		if message := result.frame.GetMessage(); message != nil && message.GetOpcode() == executorpb.ExecutorMessage_OPCODE_TEXT {
			return message.GetPayload(), nil
		}
		if closed := result.frame.GetClose(); closed != nil {
			return nil, fmt.Errorf("executor Gateway closed channel (%d): %s", closed.GetCode(), closed.GetReason())
		}
		return nil, fmt.Errorf("unexpected executor Gateway frame %T", result.frame.GetFrame())
	case <-ctx.Done():
		s.cancel()
		result := <-completed
		if result.err != nil {
			return nil, result.err
		}
		return nil, ctx.Err()
	}
}

func (s *gatewayExecutorSocket) Close(_ context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.cancel()
	return s.stream.CloseSend()
}

type directExecutorSocket struct{ connection *websocket.Conn }

func (s *directExecutorSocket) Send(ctx context.Context, payload []byte) error {
	return s.connection.Write(ctx, websocket.MessageText, payload)
}

func (s *directExecutorSocket) Receive(ctx context.Context) ([]byte, error) {
	kind, payload, err := s.connection.Read(ctx)
	if err != nil {
		return nil, err
	}
	if kind != websocket.MessageText {
		return nil, fmt.Errorf("unexpected executor WebSocket message type %v", kind)
	}
	return payload, nil
}

func (s *directExecutorSocket) Close(ctx context.Context) error {
	return s.connection.Close(websocket.StatusNormalClosure, "")
}

func gatewayHandshakeSend(ctx context.Context, stream grpc.BidiStreamingClient[executorpb.ExecutorFrame, executorpb.ExecutorFrame], frame *executorpb.ExecutorFrame, cancel context.CancelFunc) error {
	completed := make(chan error, 1)
	go func() { completed <- stream.Send(frame) }()
	select {
	case err := <-completed:
		return err
	case <-ctx.Done():
		cancel()
		return <-completed
	}
}

func gatewayHandshakeReceive(ctx context.Context, stream grpc.BidiStreamingClient[executorpb.ExecutorFrame, executorpb.ExecutorFrame]) (*executorpb.ExecutorFrame, error) {
	completed := make(chan struct {
		frame *executorpb.ExecutorFrame
		err   error
	}, 1)
	go func() {
		frame, err := stream.Recv()
		completed <- struct {
			frame *executorpb.ExecutorFrame
			err   error
		}{frame, err}
	}()
	select {
	case result := <-completed:
		return result.frame, result.err
	case <-ctx.Done():
		return nil, ctx.Err()
	}
}

type Executor struct {
	store       *Store
	owner       Owner
	socket      executorSocket
	cwd         string
	codexPath   string
	environment map[string]string
	// shell is the executor-reported default shell; nil when the exec-server reports none.
	shell      *execShell
	rpcMu      sync.Mutex
	dispatchMu sync.Mutex
	stopping   bool
	// reconnectRequired fences all future operations until this exact exec-server
	// session has been resumed on a new transport.
	reconnectRequired bool
	processMu         sync.Mutex
	processLocks      map[string]*sync.Mutex
	rpcTimeout        time.Duration
	lifetime          context.Context
	openSocket        func(context.Context, context.Context) (executorSocket, func() error, error)
	closeSocket       func() error
}

func ConnectExecutor(ctx context.Context, store *Store, owner Owner, session *Session, settings Settings) (*Executor, func() error, error) {
	request := &samplerpb.OpenSessionRequest{}
	if err := unmarshalProto(session.Configuration, request); err != nil {
		return nil, nil, fmt.Errorf("decode stored sampler Session: %w", err)
	}
	openSocket := func(lifetime, ctx context.Context) (executorSocket, func() error, error) {
		var socket executorSocket
		var closeTransport func() error
		if settings.GatewayTarget != "" {
			address, err := settings.GatewayAddress(request.GetExecutor().GetOwner().GetNamespace(), request.GetExecutor().GetSandbox())
			if err != nil {
				return nil, nil, err
			}
			connection, err := bearer.Dial(address, settings.GatewayTokenFile, time.Duration(settings.RPCTimeoutSeconds)*time.Second)
			if err != nil {
				return nil, nil, err
			}
			streamCtx, cancelStream := context.WithCancel(lifetime)
			stream, err := executorpb.NewExecutorGatewayClient(connection).Connect(streamCtx)
			if err != nil {
				cancelStream()
				_ = connection.Close()
				return nil, nil, err
			}
			socket = &gatewayExecutorSocket{stream: stream, cancel: cancelStream}
			openCtx, cancelOpen := context.WithTimeout(ctx, time.Duration(settings.RPCTimeoutSeconds)*time.Second)
			open := &executorpb.ExecutorFrame{Frame: &executorpb.ExecutorFrame_Open{Open: &executorpb.ExecutorOpen{
				Destination: request.GetExecutor(), ExpectedIncarnationUid: request.GetExpectedIncarnationUid(), SessionId: session.ID,
			}}}
			if err := gatewayHandshakeSend(openCtx, stream, open, cancelStream); err != nil {
				cancelOpen()
				cancelStream()
				_ = connection.Close()
				return nil, nil, fmt.Errorf("send executor open to Runner Gateway: %w", err)
			}
			cancelOpen()
			// Runner startup is lazy and can take longer than an individual RPC. The Runner and
			// Gateway already bound readiness; keep this wait under the caller's setup/turn budget.
			ready, err := gatewayHandshakeReceive(ctx, stream)
			if err != nil {
				cancelStream()
				_ = connection.Close()
				return nil, nil, fmt.Errorf("wait for Runner Gateway executor readiness: %w", err)
			}
			if ready.GetReady() == nil || !ready.GetReady().GetAccepted() {
				cancelStream()
				_ = connection.Close()
				return nil, nil, errors.New("executor Gateway refused Sandbox connection")
			}
			closeTransport = func() error {
				_ = socket.Close(context.Background())
				cancelStream()
				return connection.Close()
			}
		} else {
			dialCtx, cancelDial := context.WithTimeout(ctx, time.Duration(settings.RPCTimeoutSeconds)*time.Second)
			connection, _, err := websocket.Dial(dialCtx, settings.DirectExecutorURL, nil)
			cancelDial()
			if err != nil {
				return nil, nil, err
			}
			socket = &directExecutorSocket{connection: connection}
			closeTransport = func() error { return socket.Close(context.Background()) }
		}
		return socket, closeTransport, nil
	}
	socket, closeTransport, err := openSocket(ctx, ctx)
	if err != nil {
		return nil, nil, err
	}
	env, err := json.Marshal(map[string]any{"destination_ref": map[string]any{
		"namespace": request.GetExecutor().GetOwner().GetNamespace(), "name": request.GetExecutor().GetSandbox(), "uid": request.GetExecutor().GetSandboxUid(),
	}, "session_id": session.ID})
	if err != nil {
		_ = closeTransport()
		return nil, nil, err
	}
	executor := &Executor{
		store: store, owner: owner, socket: socket, lifetime: ctx, openSocket: openSocket, closeSocket: closeTransport, cwd: request.GetSpec().GetCwd(), codexPath: settings.ExecutorCodex,
		environment: map[string]string{"AGENTPLANE_DESTINATION": string(env)}, processLocks: map[string]*sync.Mutex{},
		rpcTimeout: time.Duration(settings.RPCTimeoutSeconds) * time.Second,
	}
	setupCtx, cancelSetup := context.WithTimeout(ctx, time.Duration(settings.RPCTimeoutSeconds)*time.Second)
	defer cancelSetup()
	if err := executor.initialize(setupCtx); err != nil {
		_ = executor.close()
		return nil, nil, err
	}
	if !session.WorkspacePrepared {
		input, err := json.Marshal(map[string]string{"cmd": "mkdir -p -- " + shellQuote(request.GetSpec().GetCwd()), "workdir": "/"})
		if err != nil {
			_ = executor.close()
			return nil, nil, err
		}
		result, err := executor.execCommand(setupCtx, input)
		if err != nil {
			_ = executor.close()
			return nil, nil, err
		}
		if result.ExitCode == nil || *result.ExitCode != 0 {
			_ = executor.close()
			return nil, nil, errors.New("executor workspace preparation did not complete")
		}
		if err := store.PreparedWorkspace(setupCtx, owner, session.ID); err != nil {
			_ = executor.close()
			return nil, nil, err
		}
	}
	return executor, executor.close, nil
}

func unmarshalProto(data []byte, message interface {
	Reset()
	String() string
	ProtoMessage()
},
) error {
	return proto.Unmarshal(data, message.(proto.Message))
}

func (e *Executor) initialize(ctx context.Context) error {
	state, err := e.store.ExecutionState(ctx, e.owner)
	if err != nil {
		return err
	}
	needsRemote := len(state.Cells) > 0
	for _, process := range state.Processes {
		if process.Status != "exited_unread" && process.Status != "abandoned" {
			needsRemote = true
		}
	}
	conversation, err := e.store.Read(ctx, e.owner)
	if err != nil {
		return err
	}
	var resume *string
	if needsRemote && conversation.ExecutorSession != nil {
		resume = conversation.ExecutorSession
	}
	result, err := executorRPC[executorInitialized](ctx, e, "initialize", executorInitialize{ClientName: "central-sampler", ResumeSessionID: resume})
	if err != nil {
		return err
	}
	sessionID := result.SessionID
	if sessionID == "" {
		return errors.New("exec-server initialized without a session ID")
	}
	var shellName *string
	if result.EnvironmentInfo != nil {
		e.shell = &result.EnvironmentInfo.Shell
		shellName = &e.shell.Name
	}
	if err := e.store.SetExecutorSession(ctx, e.owner, sessionID, shellName); err != nil {
		return err
	}
	return e.send(ctx, executorRequest[executorEmpty]{Method: "initialized", Params: executorEmpty{}})
}

func (e *Executor) send(ctx context.Context, value any) error {
	data, err := json.Marshal(value)
	if err != nil {
		return err
	}
	return e.socket.Send(ctx, data)
}

// rpc journals each call as a nested intent. resultEvidence derives the journaled result from
// the decoded reply object; nil journals the reply as received.
func (e *Executor) rpc(ctx context.Context, method string, params any, resultEvidence func(map[string]any) map[string]any) (json.RawMessage, error) {
	e.rpcMu.Lock()
	defer e.rpcMu.Unlock()
	if e.reconnectRequired {
		if err := e.reconnectLocked(ctx); err != nil {
			return nil, &executorNotDispatchedError{method: method, processID: executorProcessID(params), retainedSession: e.retainedSessionID(ctx), cause: err}
		}
	}
	return e.rpcLocked(ctx, method, params, resultEvidence)
}

func (e *Executor) rpcLocked(ctx context.Context, method string, params any, resultEvidence func(map[string]any) map[string]any) (json.RawMessage, error) {
	if e.rpcTimeout > 0 {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(ctx, e.rpcTimeout)
		defer cancel()
	}
	id := uuid.NewString()
	request, err := protocolObject(struct {
		Kind   string `json:"kind"`
		Method string `json:"method"`
		Params any    `json:"params"`
	}{"executor", method, params})
	if err != nil {
		return nil, err
	}
	intent, err := e.store.RecordNested(ctx, e.owner, request)
	if err != nil {
		return nil, err
	}
	processID := executorProcessID(params)
	reply, err := e.exchange(ctx, id, method, params)
	var transport *executorTransportError
	if method == "process/read" && errors.As(err, &transport) && ctx.Err() == nil && e.openSocket != nil {
		if evidenceErr := e.store.ProtocolEvent(ctx, e.owner, intent, "executor", "reconnecting", map[string]any{"method": method, "process_id": processID, "reason": err.Error()}); evidenceErr != nil {
			transport.intentID, transport.processID = intent, processID
			if finishErr := e.finishUnknownIntent(ctx, intent, transport.evidence(e.retainedSessionID(ctx))); finishErr != nil {
				return nil, errors.Join(evidenceErr, fmt.Errorf("record unknown exec-server %s outcome: %w", method, finishErr))
			}
			return nil, transport
		}
		if reconnectErr := e.reconnectLocked(ctx); reconnectErr != nil {
			transport.cause = errors.Join(transport.cause, fmt.Errorf("resume executor session: %w", reconnectErr))
			transport.intentID, transport.processID = intent, processID
			if finishErr := e.finishUnknownIntent(ctx, intent, transport.evidence(e.retainedSessionID(ctx))); finishErr != nil {
				return nil, fmt.Errorf("record unknown exec-server %s outcome: %w", method, finishErr)
			}
			return nil, transport
		}
		// process/read is cursor based and does not consume output. Reuse the
		// intent, process ID and cursor; never start the shell command again.
		reply, err = e.exchange(ctx, id, method, params)
	}
	if err != nil {
		if errors.As(err, &transport) {
			transport.intentID, transport.processID = intent, processID
			e.reconnectRequired = true
			if finishErr := e.finishUnknownIntent(ctx, intent, transport.evidence(e.retainedSessionID(ctx))); finishErr != nil {
				return nil, fmt.Errorf("record unknown exec-server %s outcome: %w", method, finishErr)
			}
		}
		return nil, err
	}
	if reply.Error != nil {
		rejection := map[string]any{"rejected": map[string]any{"code": reply.Error.Code, "message": reply.Error.Message}}
		if err := e.store.FinishNested(ctx, e.owner, intent, rejection); err != nil {
			return nil, err
		}
		return nil, &executorRejectedError{method: method, code: reply.Error.Code, message: reply.Error.Message}
	}
	var evidence map[string]any
	if err := decodeProtocolObject(reply.Result, &evidence); err != nil {
		return nil, fmt.Errorf("exec-server %s result: %w", method, err)
	}
	if resultEvidence != nil {
		evidence = resultEvidence(evidence)
	}
	if err := e.store.FinishNested(ctx, e.owner, intent, evidence); err != nil {
		return nil, err
	}
	return reply.Result, nil
}

type executorTransportError struct {
	method          string
	phase           string
	intentID        string
	processID       string
	retainedSession string
	afterSequence   *uint64
	output          *headTailBuffer
	cause           error
}

func (e *executorTransportError) Error() string {
	return fmt.Sprintf("exec-server %s %s transport failed (intent %s%s): remote effect outcome is unknown; mutation was not replayed: %v",
		e.method, e.phase, e.intentID, processDescription(e.processID), e.cause)
}

func (e *executorTransportError) Unwrap() error { return e.cause }

func (e *executorTransportError) evidence(retainedSession string) map[string]any {
	e.retainedSession = retainedSession
	result := map[string]any{
		"status": "abandoned", "remote_outcome": "unknown", "automatic_replay": false,
		"method": e.method, "phase": e.phase, "intent_id": e.intentID, "transport_error": e.cause.Error(),
	}
	if e.processID != "" {
		result["process_id"] = e.processID
	}
	if retainedSession != "" {
		result["executor_session_id"] = retainedSession
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
	method          string
	processID       string
	retainedSession string
	cause           error
}

func (e *executorNotDispatchedError) Error() string {
	return fmt.Sprintf("exec-server %s was not dispatched%s because the retained session could not be resumed: %v; previous remote outcomes remain unknown and no mutation was replayed",
		e.method, processDescription(e.processID), e.cause)
}

func (e *executorNotDispatchedError) Unwrap() error { return e.cause }

func (e *executorNotDispatchedError) evidence() map[string]any {
	result := map[string]any{"status": "abandoned", "remote_outcome": "not_dispatched", "automatic_replay": false, "method": e.method, "transport_error": e.cause.Error()}
	if e.processID != "" {
		result["process_id"] = e.processID
	}
	if e.retainedSession != "" {
		result["executor_session_id"] = e.retainedSession
	}
	return result
}

func processDescription(id string) string {
	if id == "" {
		return ""
	}
	return " for process " + id
}

func executorProcessID(params any) string {
	switch value := params.(type) {
	case executorStart:
		return value.ProcessID
	case executorRead:
		return value.ProcessID
	case executorWrite:
		return value.ProcessID
	case executorSignal:
		return value.ProcessID
	case executorTerminate:
		return value.ProcessID
	default:
		return ""
	}
}

func (e *Executor) retainedSessionID(ctx context.Context) string {
	if ctx.Err() != nil {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
	}
	conversation, err := e.store.Read(ctx, e.owner)
	if err != nil || conversation.ExecutorSession == nil {
		return ""
	}
	return *conversation.ExecutorSession
}

func (e *Executor) finishUnknownIntent(ctx context.Context, intent string, evidence map[string]any) error {
	if ctx.Err() != nil {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
	}
	return e.store.FinishNested(ctx, e.owner, intent, evidence)
}

func (e *Executor) exchange(ctx context.Context, id, method string, params any) (*executorReply, error) {
	if err := e.send(ctx, executorRequest[any]{ID: id, Method: method, Params: params}); err != nil {
		return nil, &executorTransportError{method: method, phase: "send", cause: err}
	}
	for {
		data, err := e.socket.Receive(ctx)
		if err != nil {
			return nil, &executorTransportError{method: method, phase: "receive", cause: err}
		}
		var reply executorReply
		if err := decodeProtocolObject(data, &reply); err != nil {
			return nil, fmt.Errorf("decode exec-server message: %w", err)
		}
		if reply.ID == nil {
			switch reply.Method {
			case "process/output", "process/exited", "process/closed":
				continue
			default:
				return nil, fmt.Errorf("unexpected exec-server notification %q", reply.Method)
			}
		}
		if *reply.ID != id {
			return nil, fmt.Errorf("unexpected exec-server response ID for %s", method)
		}
		return &reply, nil
	}
}

// reconnectLocked resumes exactly the retained executor session, under the original
// Sandbox incarnation fence. An expired session is a failure, never a new session.
// The caller holds rpcMu, so initialize is sent through rpcLocked without recursion.
func (e *Executor) reconnectLocked(ctx context.Context) error {
	e.reconnectRequired = true
	if e.openSocket == nil {
		return errors.New("executor transport cannot be reopened")
	}
	conversation, err := e.store.Read(ctx, e.owner)
	if err != nil {
		return err
	}
	if conversation.ExecutorSession == nil {
		return errors.New("cannot resume executor without a retained session ID")
	}
	if e.closeSocket != nil {
		_ = e.closeSocket()
	}
	socket, closeSocket, err := e.openSocket(e.lifetime, ctx)
	if err != nil {
		return err
	}
	e.socket, e.closeSocket = socket, closeSocket
	raw, err := e.rpcLocked(ctx, "initialize", executorInitialize{ClientName: "central-sampler", ResumeSessionID: conversation.ExecutorSession}, nil)
	if err != nil {
		return err
	}
	var initialized executorInitialized
	if err := decodeProtocolObject(raw, &initialized); err != nil {
		return err
	}
	if initialized.SessionID != *conversation.ExecutorSession {
		return errors.New("exec-server resumed a different session")
	}
	if err := e.send(ctx, executorRequest[executorEmpty]{Method: "initialized", Params: executorEmpty{}}); err != nil {
		return err
	}
	e.reconnectRequired = false
	return nil
}

func (e *Executor) close() error {
	e.rpcMu.Lock()
	defer e.rpcMu.Unlock()
	return e.closeSocket()
}

// Invoke runs one nested tool call from JavaScript. A *toolError is reported to the cell as a
// rejected call; any other error leaves the remote outcome uncertain.
func (e *Executor) Invoke(ctx context.Context, name string, arguments json.RawMessage) (any, error) {
	var result any
	var err error
	switch name {
	case "exec_command":
		result, err = e.execCommand(ctx, arguments)
	case "write_stdin":
		result, err = e.writeStdin(ctx, arguments)
	case "apply_patch":
		result, err = e.applyPatch(ctx, arguments)
	case "view_image":
		result, err = e.viewImage(ctx, arguments)
	default:
		return nil, fmt.Errorf("unsupported nested tool %q", name)
	}
	if err != nil {
		return nil, e.outcomeToolError(ctx, err)
	}
	return result, nil
}

func (e *Executor) outcomeToolError(ctx context.Context, err error) error {
	if ctx.Err() != nil {
		var cancel context.CancelFunc
		ctx, cancel = context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
	}
	var transport *executorTransportError
	if errors.As(err, &transport) {
		if transport.processID != "" {
			if abandonErr := e.store.AbandonProcess(ctx, e.owner, transport.processID, transport.afterSequence, transport.output, "unknown", transport.retainedSession); abandonErr != nil {
				return fmt.Errorf("record unknown process %s outcome after %s transport failure: %w", transport.processID, transport.method, abandonErr)
			}
		}
		return &toolOutcomeError{toolError: &toolError{transport.Error()}, evidence: transport.evidence(transport.retainedSession)}
	}
	var notDispatched *executorNotDispatchedError
	if errors.As(err, &notDispatched) {
		outcome := "unknown"
		if notDispatched.processID != "" {
			process, processErr := e.store.Process(ctx, e.owner, notDispatched.processID)
			if processErr != nil {
				return fmt.Errorf("inspect process %s after failed executor reconnect: %w", notDispatched.processID, processErr)
			}
			if process.Status == "starting" {
				outcome = "not_dispatched"
			}
			if abandonErr := e.store.AbandonProcess(ctx, e.owner, notDispatched.processID, process.AfterSequence, nil, outcome, notDispatched.retainedSession); abandonErr != nil {
				return fmt.Errorf("record process %s after failed executor reconnect: %w", notDispatched.processID, abandonErr)
			}
		}
		return &toolOutcomeError{toolError: &toolError{notDispatched.Error()}, evidence: notDispatched.evidence()}
	}
	return err
}

// Codex rust-v0.162.1 core/src/tools/handlers/unified_exec/exec_command.rs handle_call and
// core/src/unified_exec/process_manager.rs exec_command_inner.
func (e *Executor) execCommand(ctx context.Context, arguments json.RawMessage) (*unifiedExecResult, error) {
	plan, err := planExecCommand(arguments, e.cwd, e.shell)
	if err != nil {
		return nil, err
	}
	environment := maps.Clone(e.environment)
	maps.Copy(environment, unifiedExecEnvironment)
	processID, sessionID, unlock, err := e.startProcess(ctx, plan.argv, plan.cwd, environment, plan.tty)
	if err != nil {
		var rejected *executorRejectedError
		if errors.As(err, &rejected) {
			return nil, &toolError{fmt.Sprintf("exec_command failed: CreateProcess { message: %q }", rejected.message)}
		}
		return nil, err
	}
	defer unlock()
	observation, err := e.readProcess(ctx, processID, plan.yieldMS)
	if err != nil {
		return nil, err
	}
	return observation.unifiedExecResult(&sessionID, plan.maxOutputTokens, newChunkID()), nil
}

// applyPatchResult is what a nested apply_patch call resolves to in JavaScript.
type applyPatchResult struct {
	Output    string `json:"output"`
	ExitCode  *int   `json:"exit_code"`
	SessionID *int   `json:"session_id,omitempty"`
}

func (e *Executor) applyPatch(ctx context.Context, arguments json.RawMessage) (*applyPatchResult, error) {
	var patch string
	if err := json.Unmarshal(arguments, &patch); err != nil || bytes.Equal(bytes.TrimSpace(arguments), []byte("null")) {
		return nil, &toolError{"apply_patch takes a patch string"}
	}
	processID, sessionID, unlock, err := e.startProcess(ctx, []string{e.codexPath, "--codex-run-as-apply-patch", patch}, e.cwd, e.environment, false)
	if err != nil {
		return nil, err
	}
	defer unlock()
	observation, err := e.readProcess(ctx, processID, 60000)
	if err != nil {
		return nil, err
	}
	result := &applyPatchResult{Output: lossyUTF8(observation.output.withOmissionMarker()), ExitCode: observation.exitCode}
	if !observation.exited {
		result.SessionID = &sessionID
	}
	return result, nil
}

// startProcess registers a model-visible process under a fresh unified exec session number and
// launches it. The returned unlock releases the process lock held from registration on.
// Codex rust-v0.162.1 core/src/unified_exec/process_manager.rs allocate_process_id: random in
// [1000, 100000), unused by unreleased processes; the exec-server process ID embeds it.
func (e *Executor) startProcess(ctx context.Context, argv []string, cwd string, environment map[string]string, tty bool) (string, int, func(), error) {
	e.dispatchMu.Lock()
	defer e.dispatchMu.Unlock()
	if e.stopping {
		return "", 0, nil, errors.New("process dispatch stopped by interrupt")
	}
	var processID string
	var sessionID int
	var lock *sync.Mutex
	for {
		sessionID = 1_000 + rand.IntN(99_000)
		processID = fmt.Sprintf("%d-%s", sessionID, uuid.NewString())
		lock = e.processLock(processID)
		lock.Lock()
		err := e.store.RegisterModelProcess(ctx, e.owner, processID, sessionID, tty)
		if err == nil {
			break
		}
		lock.Unlock()
		if !errors.Is(err, ErrConflict) {
			return "", 0, nil, err
		}
	}
	_, err := executorRPC[executorStarted](ctx, e, "process/start", executorStart{ProcessID: processID, Argv: argv, Cwd: fileURI(cwd), EnvPolicy: e.envPolicy(), Env: environment, TTY: tty})
	if err != nil {
		var rejected *executorRejectedError
		if errors.As(err, &rejected) {
			// No process exists; release its session number.
			err = errors.Join(err, e.store.ProcessResult(ctx, e.owner, processID, nil, "exited", nil, true))
		}
		lock.Unlock()
		return "", 0, nil, err
	}
	if err := e.store.ProcessResult(ctx, e.owner, processID, nil, "running", nil, false); err != nil {
		lock.Unlock()
		return "", 0, nil, err
	}
	return processID, sessionID, lock.Unlock, nil
}

// envPolicy makes processes inherit the executor's environment, as wrapped Codex's do: Codex's
// default shell environment policy (inherit all, default excludes ignored) with the Session's
// variables set. Without a policy the exec-server passes only `env`, so PATH and HOME are missing.
// Codex rust-v0.162.1 config/src/shell_environment_policy.rs From<ShellEnvironmentPolicyToml>.
func (e *Executor) envPolicy() *executorEnvPolicy {
	return &executorEnvPolicy{Inherit: "all", IgnoreDefaultExcludes: true, Exclude: []string{}, Set: e.environment, IncludeOnly: []string{}}
}

// newChunkID is six random hex digits.
// Codex rust-v0.162.1 core/src/unified_exec/mod.rs generate_chunk_id.
func newChunkID() string {
	return fmt.Sprintf("%06x", rand.IntN(1<<24))
}

func (e *Executor) Setup(ctx context.Context, script string, output func(string, []byte) error) (int, error) {
	processID := uuid.NewString()
	if err := e.store.RegisterProcess(ctx, e.owner, processID, false); err != nil {
		return 0, err
	}
	if _, err := executorRPC[executorStarted](ctx, e, "process/start", executorStart{ProcessID: processID, Argv: []string{"/bin/sh", "-eu", "-c", script}, Cwd: fileURI(e.cwd), EnvPolicy: e.envPolicy(), Env: e.environment}); err != nil {
		return 0, err
	}
	if err := e.store.ProcessResult(ctx, e.owner, processID, nil, "running", nil, false); err != nil {
		return 0, err
	}
	var after *uint64
	for {
		pageCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
		result, err := executorRPC[executorReadResult](pageCtx, e, "process/read", executorRead{ProcessID: processID, AfterSeq: after, MaxBytes: 65536, WaitMS: 1000})
		cancel()
		if err != nil {
			return 0, err
		}
		chunks := result.Chunks
		for _, chunk := range chunks {
			if err := output(chunk.Stream, chunk.Chunk); err != nil {
				return 0, err
			}
		}
		if result.NextSeq > 0 {
			sequence := result.NextSeq - 1
			after = &sequence
		}
		if result.uncertain() {
			_ = e.store.ProcessResult(ctx, e.owner, processID, after, "unknown", nil, false)
			return 0, fmt.Errorf("setup process outcome unknown: %v", result.Failure)
		}
		if result.drained() {
			exitCode := result.ExitCode
			if exitCode == nil {
				return 0, errors.New("setup process exited without an exit code")
			}
			if err := e.store.ProcessResult(ctx, e.owner, processID, after, "exited", exitCode, false); err != nil {
				return 0, err
			}
			return *exitCode, nil
		}
	}
}

// Codex rust-v0.162.1 core/src/tools/handlers/unified_exec/write_stdin.rs handle_call and
// core/src/unified_exec/process_manager.rs write_stdin_inner.
func (e *Executor) writeStdin(ctx context.Context, arguments json.RawMessage) (*unifiedExecResult, error) {
	plan, err := planWriteStdin(arguments)
	if err != nil {
		return nil, err
	}
	process, err := e.store.ProcessByModelSession(ctx, e.owner, plan.sessionID)
	if errors.Is(err, ErrMissing) {
		return nil, &toolError{fmt.Sprintf("write_stdin failed: Unknown process id %d", plan.sessionID)}
	}
	if err != nil {
		return nil, err
	}
	lock := e.processLock(process.ID)
	lock.Lock()
	defer lock.Unlock()
	// Revalidate under the lock: the number may have been released and reused meanwhile.
	process, err = e.store.ProcessByModelSession(ctx, e.owner, plan.sessionID)
	if errors.Is(err, ErrMissing) {
		return nil, &toolError{fmt.Sprintf("write_stdin failed: Unknown process id %d", plan.sessionID)}
	}
	if err != nil {
		return nil, err
	}
	if process.Status != "running" && process.Status != "exited_unread" {
		if process.Status == "abandoned" {
			return nil, &toolError{fmt.Sprintf("write_stdin failed: process %d has an unknown remote outcome and its handle is abandoned; no input was sent", plan.sessionID)}
		}
		return nil, fmt.Errorf("session %d is %s; its remote outcome is unknown", plan.sessionID, process.Status)
	}
	if plan.chars != "" {
		if !process.TTY {
			if plan.chars != interruptInput {
				return nil, &toolError{"write_stdin failed: " + stdinClosedMessage}
			}
			if process.Status == "running" {
				if err := e.dispatchInput(ctx, func() error {
					_, err := executorRPC[executorEmpty](ctx, e, "process/signal", executorSignal{ProcessID: process.ID, Signal: "interrupt"})
					return err
				}); err != nil {
					return nil, err
				}
			}
		} else if process.Status == "running" {
			// A write to an exited terminal is moot; the poll below reports the exit.
			if err := e.dispatchInput(ctx, func() error {
				result, err := executorRPC[executorWriteResult](ctx, e, "process/write", executorWrite{ProcessID: process.ID, Chunk: []byte(plan.chars), WriteID: uuid.NewString()})
				if err != nil {
					return err
				}
				if result.Status != "accepted" {
					return &toolError{"write_stdin failed: " + writeToStdinFailedMessage}
				}
				return nil
			}); err != nil {
				return nil, err
			}
			// Give the process a brief window to react, as upstream does, before polling.
			select {
			case <-time.After(100 * time.Millisecond):
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
	}
	observation, err := e.readProcess(ctx, process.ID, plan.yieldMS)
	if err != nil {
		return nil, err
	}
	return observation.unifiedExecResult(process.ModelSessionID, plan.maxOutputTokens, newChunkID()), nil
}

func (e *Executor) dispatchInput(ctx context.Context, send func() error) error {
	e.dispatchMu.Lock()
	defer e.dispatchMu.Unlock()
	if e.stopping {
		return errors.New("stdin dispatch stopped by interrupt")
	}
	return send()
}

// readProcess collects output until the process exits or the yield deadline passes. The caller
// holds the process lock.
func (e *Executor) readProcess(ctx context.Context, processID string, yieldMS int) (*processObservation, error) {
	process, err := e.store.Process(ctx, e.owner, processID)
	if err != nil {
		return nil, err
	}
	output, err := restoreHeadTailBuffer(unifiedExecOutputMaxBytes, process.BufferedOutput, process.OmittedBytes)
	if err != nil {
		return nil, err
	}
	after := process.AfterSequence
	if process.Status == "exited_unread" {
		if err := e.store.ProcessResult(ctx, e.owner, processID, after, "exited", process.ExitCode, true); err != nil {
			return nil, err
		}
		return &processObservation{output: output, exitCode: process.ExitCode, exited: true}, nil
	}
	start := time.Now()
	deadline := start.Add(time.Duration(yieldMS) * time.Millisecond)
	for {
		waitMS := min(max(int(time.Until(deadline)/time.Millisecond), 0), 1000)
		pageCtx, cancel := context.WithTimeout(ctx, time.Duration(waitMS+eRPCSlackMS)*time.Millisecond)
		result, rpcErr := executorRPC[executorReadResult](pageCtx, e, "process/read", executorRead{ProcessID: processID, AfterSeq: after, MaxBytes: 65536, WaitMS: waitMS})
		cancel()
		if rpcErr != nil {
			var transport *executorTransportError
			if errors.As(rpcErr, &transport) {
				transport.afterSequence, transport.output = after, output
			}
			return nil, rpcErr
		}
		for _, chunk := range result.Chunks {
			output.push(chunk.Chunk)
		}
		if result.NextSeq > 0 {
			seq := result.NextSeq - 1
			after = &seq
		}
		if result.uncertain() {
			_ = e.store.ProcessResult(ctx, e.owner, processID, after, "unknown", nil, false)
			return nil, fmt.Errorf("remote process outcome unknown: %v", result.Failure)
		}
		if result.drained() {
			if err := e.store.ProcessResult(ctx, e.owner, processID, after, "exited", result.ExitCode, true); err != nil {
				return nil, err
			}
			return &processObservation{output: output, exitCode: result.ExitCode, exited: true, wallTime: time.Since(start).Seconds()}, nil
		}
		if !result.Exited && !time.Now().Before(deadline) {
			if err := e.store.ProcessResult(ctx, e.owner, processID, after, "running", nil, true); err != nil {
				return nil, err
			}
			return &processObservation{output: output, wallTime: time.Since(start).Seconds()}, nil
		}
	}
}

const eRPCSlackMS = 5000

func (e *Executor) ReconcileProcesses(ctx context.Context, timeout time.Duration) (bool, error) {
	state, err := e.store.ExecutionState(ctx, e.owner)
	if err != nil {
		return false, err
	}
	progressed := false
	for _, process := range state.Processes {
		if process.Status == "exited" || process.Status == "exited_unread" {
			continue
		}
		lock := e.processLock(process.ID)
		if !lock.TryLock() {
			continue
		}
		row, err := e.store.Process(ctx, e.owner, process.ID)
		if err != nil {
			lock.Unlock()
			return progressed, err
		}
		if row.Status == "abandoned" || row.Status == "exited" || row.Status == "exited_unread" {
			lock.Unlock()
			continue
		}
		if row.Status != "running" {
			lock.Unlock()
			return progressed, fmt.Errorf("cannot reconcile remote process in state %s", row.Status)
		}
		pageCtx, cancel := context.WithTimeout(ctx, timeout)
		result, rpcErr := executorRPC[executorReadResult](pageCtx, e, "process/read", executorRead{ProcessID: row.ID, AfterSeq: row.AfterSequence, MaxBytes: 65536})
		cancel()
		if rpcErr != nil {
			var transport *executorTransportError
			var notDispatched *executorNotDispatchedError
			if errors.As(rpcErr, &transport) || errors.As(rpcErr, &notDispatched) {
				if transport != nil {
					transport.afterSequence = row.AfterSequence
				}
				handled := e.outcomeToolError(ctx, rpcErr)
				var toolOutcome *toolOutcomeError
				if !errors.As(handled, &toolOutcome) {
					lock.Unlock()
					return progressed, handled
				}
				lock.Unlock()
				return true, nil
			}
			lock.Unlock()
			return progressed, rpcErr
		}
		if result.uncertain() {
			_ = e.store.ProcessResult(ctx, e.owner, row.ID, row.AfterSequence, "unknown", nil, false)
			lock.Unlock()
			return progressed, fmt.Errorf("remote process outcome unknown: %v", result.Failure)
		}
		chunks := result.Chunks
		output := result.output()
		afterValue := result.NextSeq
		if len(chunks) > 0 || result.Exited {
			seq := uint64(0)
			if afterValue > 0 {
				seq = uint64(afterValue - 1)
			}
			exitCode := result.ExitCode
			if err := e.store.RetainProcessOutput(ctx, e.owner, row.ID, seq, output, result.drained(), exitCode); err != nil {
				lock.Unlock()
				return progressed, err
			}
			progressed = true
		}
		lock.Unlock()
	}
	return progressed, nil
}

func (e *Executor) InterruptAll(ctx context.Context) (map[string]any, error) {
	e.dispatchMu.Lock()
	e.stopping = true
	state, err := e.store.ExecutionState(ctx, e.owner)
	e.dispatchMu.Unlock()
	if err != nil {
		return nil, err
	}
	outcomes := map[string]any{}
	for _, process := range state.Processes {
		if process.Status == "exited_unread" || process.Status == "exited" {
			continue
		}
		intent, err := e.store.RecordNested(ctx, e.owner, map[string]any{"kind": "interrupt_process", "session_id": process.ID})
		if err != nil {
			return outcomes, err
		}
		requestCtx, cancel := context.WithTimeout(ctx, 5*time.Second)
		_, terminateErr := executorRPC[executorTerminateResult](requestCtx, e, "process/terminate", executorTerminate{ProcessID: process.ID})
		cancel()
		if terminateErr == nil {
			lock := e.processLock(process.ID)
			lock.Lock()
			result, readErr := e.readProcess(ctx, process.ID, 3000)
			lock.Unlock()
			if readErr == nil && result.exited {
				outcomes[process.ID] = map[string]any{"exit_observed": true, "result": map[string]any{
					"output": lossyUTF8(result.output.withOmissionMarker()), "exit_code": result.exitCode,
				}}
				if err := e.store.FinishNested(ctx, e.owner, intent, outcomes[process.ID].(map[string]any)); err != nil {
					return outcomes, err
				}
				continue
			}
			terminateErr = readErr
		}
		outcomes[process.ID] = map[string]any{"exit_observed": false, "effects_unknown": true, "error": fmt.Sprint(terminateErr)}
	}
	return outcomes, nil
}

func (e *Executor) processLock(id string) *sync.Mutex {
	e.processMu.Lock()
	defer e.processMu.Unlock()
	if e.processLocks[id] == nil {
		e.processLocks[id] = &sync.Mutex{}
	}
	return e.processLocks[id]
}

func shellQuote(value string) string {
	return "'" + strings.ReplaceAll(value, "'", "'\\''") + "'"
}

func fileURI(path string) string {
	return (&url.URL{Scheme: "file", Path: path}).String()
}

func numberInt64(value any) (int64, bool) {
	switch number := value.(type) {
	case float64:
		return int64(number), number == float64(int64(number))
	case int:
		return int64(number), true
	case int64:
		return number, true
	case json.Number:
		parsed, err := number.Int64()
		return parsed, err == nil
	case uint64:
		return int64(number), number <= uint64(^uint64(0)>>1)
	default:
		return 0, false
	}
}
