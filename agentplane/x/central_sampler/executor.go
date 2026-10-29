package centralsampler

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"
	"path/filepath"
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
	store        *Store
	owner        Owner
	socket       executorSocket
	cwd          string
	codexPath    string
	environment  map[string]string
	rpcMu        sync.Mutex
	dispatchMu   sync.Mutex
	stopping     bool
	processMu    sync.Mutex
	processLocks map[string]*sync.Mutex
	rpcTimeout   time.Duration
}

func ConnectExecutor(ctx context.Context, store *Store, owner Owner, session *Session, settings Settings) (*Executor, func() error, error) {
	request := &samplerpb.OpenSessionRequest{}
	if err := unmarshalProto(session.Configuration, request); err != nil {
		return nil, nil, fmt.Errorf("decode stored sampler Session: %w", err)
	}
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
		streamCtx, cancelStream := context.WithCancel(ctx)
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
	env, err := json.Marshal(map[string]any{"destination_ref": map[string]any{
		"namespace": request.GetExecutor().GetOwner().GetNamespace(), "name": request.GetExecutor().GetSandbox(), "uid": request.GetExecutor().GetSandboxUid(),
	}, "session_id": session.ID})
	if err != nil {
		_ = closeTransport()
		return nil, nil, err
	}
	executor := &Executor{
		store: store, owner: owner, socket: socket, cwd: request.GetSpec().GetCwd(), codexPath: settings.ExecutorCodex,
		environment: map[string]string{"AGENTPLANE_DESTINATION": string(env)}, processLocks: map[string]*sync.Mutex{},
		rpcTimeout: time.Duration(settings.RPCTimeoutSeconds) * time.Second,
	}
	setupCtx, cancelSetup := context.WithTimeout(ctx, time.Duration(settings.RPCTimeoutSeconds)*time.Second)
	defer cancelSetup()
	if err := executor.initialize(setupCtx); err != nil {
		_ = closeTransport()
		return nil, nil, err
	}
	if !session.WorkspacePrepared {
		workdir := "/"
		input, err := json.Marshal(execCommandInput{Cmd: "mkdir -p -- " + shellQuote(request.GetSpec().GetCwd()), Workdir: &workdir})
		if err != nil {
			_ = closeTransport()
			return nil, nil, err
		}
		result, err := executor.Invoke(setupCtx, "exec_command", input)
		if err != nil {
			_ = closeTransport()
			return nil, nil, err
		}
		if result.ExitCode == nil || *result.ExitCode != 0 {
			_ = closeTransport()
			return nil, nil, errors.New("executor workspace preparation did not complete")
		}
		if err := store.PreparedWorkspace(setupCtx, owner, session.ID); err != nil {
			_ = closeTransport()
			return nil, nil, err
		}
	}
	return executor, closeTransport, nil
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
		if process.Status != "exited_unread" {
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
	if err := e.store.SetExecutorSession(ctx, e.owner, sessionID); err != nil {
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

func (e *Executor) rpc(ctx context.Context, method string, params any) (json.RawMessage, error) {
	e.rpcMu.Lock()
	defer e.rpcMu.Unlock()
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
	if err := e.send(ctx, executorRequest[any]{ID: id, Method: method, Params: params}); err != nil {
		return nil, err
	}
	for {
		data, err := e.socket.Receive(ctx)
		if err != nil {
			return nil, fmt.Errorf("exec-server reply lost for %s; remote effect outcome may be unknown: %w", method, err)
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
		if reply.Error != nil {
			return nil, fmt.Errorf("exec-server rejected %s (%d): %s", method, reply.Error.Code, reply.Error.Message)
		}
		var evidence map[string]any
		if err := decodeProtocolObject(reply.Result, &evidence); err != nil {
			return nil, fmt.Errorf("exec-server %s result: %w", method, err)
		}
		if err := e.store.FinishNested(ctx, e.owner, intent, evidence); err != nil {
			return nil, err
		}
		return reply.Result, nil
	}
}

func (e *Executor) Invoke(ctx context.Context, name string, arguments json.RawMessage) (*processToolResult, error) {
	var argv []string
	var cwd string
	var tty bool
	var yieldMS int
	if name == "exec_command" {
		var args execCommandInput
		if err := decodeToolInput(arguments, &args); err != nil {
			return nil, err
		}
		if args.Cmd == "" {
			return nil, errors.New("exec_command requires a command string")
		}
		argv = []string{"/bin/sh", "-c", args.Cmd}
		cwd = e.cwd
		if args.Workdir != nil {
			cwd = *args.Workdir
			if !filepath.IsAbs(cwd) {
				return nil, errors.New("workdir must be an absolute path")
			}
		}
		tty = args.TTY
		var err error
		yieldMS, err = toolYieldTime(args.YieldTimeMS, 60000)
		if err != nil {
			return nil, err
		}
	} else if name == "apply_patch" {
		var patch string
		if err := json.Unmarshal(arguments, &patch); err != nil || bytes.Equal(bytes.TrimSpace(arguments), []byte("null")) {
			return nil, errors.New("apply_patch takes a patch string")
		}
		argv, cwd, yieldMS = []string{e.codexPath, "--codex-run-as-apply-patch", patch}, e.cwd, 60000
	} else if name == "write_stdin" {
		return e.writeStdin(ctx, arguments)
	} else {
		return nil, fmt.Errorf("unsupported nested tool %q", name)
	}
	processID := uuid.NewString()
	lock := e.processLock(processID)
	lock.Lock()
	defer lock.Unlock()
	e.dispatchMu.Lock()
	if e.stopping {
		e.dispatchMu.Unlock()
		return nil, errors.New("process dispatch stopped by interrupt")
	}
	if err := e.store.RegisterProcess(ctx, e.owner, processID, tty); err != nil {
		e.dispatchMu.Unlock()
		return nil, err
	}
	_, err := executorRPC[executorStarted](ctx, e, "process/start", executorStart{ProcessID: processID, Argv: argv, Cwd: fileURI(cwd), Env: e.environment, TTY: tty, PipeStdin: !tty})
	e.dispatchMu.Unlock()
	if err != nil {
		return nil, err
	}
	if err := e.store.ProcessResult(ctx, e.owner, processID, nil, "running", nil, false); err != nil {
		return nil, err
	}
	return e.readProcess(ctx, processID, yieldMS)
}

func (e *Executor) Setup(ctx context.Context, script string, output func(string, []byte) error) (int, error) {
	processID := uuid.NewString()
	if err := e.store.RegisterProcess(ctx, e.owner, processID, false); err != nil {
		return 0, err
	}
	if _, err := executorRPC[executorStarted](ctx, e, "process/start", executorStart{ProcessID: processID, Argv: []string{"/bin/sh", "-eu", "-c", script}, Cwd: fileURI(e.cwd), Env: e.environment}); err != nil {
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
		if result.Exited && len(chunks) == 0 {
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

func (e *Executor) writeStdin(ctx context.Context, arguments json.RawMessage) (*processToolResult, error) {
	var args writeStdinInput
	if err := decodeToolInput(arguments, &args); err != nil {
		return nil, err
	}
	if args.SessionID == "" {
		return nil, errors.New("write_stdin requires session_id")
	}
	processID, chars, eof := args.SessionID, args.Chars, args.EOF
	yieldMS, err := toolYieldTime(args.YieldTimeMS, 1000)
	if err != nil {
		return nil, err
	}
	lock := e.processLock(processID)
	lock.Lock()
	defer lock.Unlock()
	process, err := e.store.Process(ctx, e.owner, processID)
	if err != nil {
		return nil, err
	}
	if process.Status != "running" && !(process.Status == "exited_unread" && chars == "" && !eof) {
		return nil, fmt.Errorf("session %s is %s; stdin was not sent", processID, process.Status)
	}
	if eof && (!process.TTY || chars != "") {
		return nil, errors.New("EOF requires a TTY and empty chars")
	}
	if chars != "" || eof {
		e.dispatchMu.Lock()
		if e.stopping {
			e.dispatchMu.Unlock()
			return nil, errors.New("stdin dispatch stopped by interrupt")
		}
		err = e.sendInput(ctx, processID, chars, eof)
		e.dispatchMu.Unlock()
		if err != nil {
			return nil, err
		}
	}
	return e.readProcess(ctx, processID, yieldMS)
}

func (e *Executor) sendInput(ctx context.Context, processID, chars string, eof bool) error {
	if chars == "\x03" {
		_, err := executorRPC[executorEmpty](ctx, e, "process/signal", executorSignal{ProcessID: processID, Signal: "interrupt"})
		return err
	}
	payload := chars
	if eof {
		payload = "\x04"
	}
	result, err := executorRPC[executorWriteResult](ctx, e, "process/write", executorWrite{ProcessID: processID, Chunk: []byte(payload), WriteID: uuid.NewString()})
	if err != nil {
		return err
	}
	if result.Status != "accepted" {
		return fmt.Errorf("stdin not accepted: %v", result.Status)
	}
	return nil
}

func (e *Executor) readProcess(ctx context.Context, processID string, yieldMS int) (*processToolResult, error) {
	process, err := e.store.Process(ctx, e.owner, processID)
	if err != nil {
		return nil, err
	}
	after := process.AfterSequence
	if process.Status == "exited_unread" {
		if err := e.store.ProcessResult(ctx, e.owner, processID, after, "exited", process.ExitCode, true); err != nil {
			return nil, err
		}
		return processOutput(process.BufferedOutput, process.ExitCode, nil), nil
	}
	deadline := time.Now().Add(time.Duration(yieldMS) * time.Millisecond)
	output := append([]byte(nil), process.BufferedOutput...)
	for {
		remaining := time.Until(deadline)
		if remaining < 0 {
			remaining = 0
		}
		waitMS := int(remaining / time.Millisecond)
		if waitMS > 1000 {
			waitMS = 1000
		}
		pageCtx, cancel := context.WithTimeout(ctx, time.Duration(waitMS+eRPCSlackMS)*time.Millisecond)
		result, rpcErr := executorRPC[executorReadResult](pageCtx, e, "process/read", executorRead{ProcessID: processID, AfterSeq: after, MaxBytes: 65536, WaitMS: waitMS})
		cancel()
		if rpcErr != nil {
			return nil, rpcErr
		}
		chunks := result.Chunks
		output = append(output, result.output()...)
		afterValue := result.NextSeq
		if afterValue > 0 {
			seq := uint64(afterValue - 1)
			after = &seq
		}
		if result.uncertain() {
			_ = e.store.ProcessResult(ctx, e.owner, processID, after, "unknown", nil, false)
			return nil, fmt.Errorf("remote process outcome unknown: %v", result.Failure)
		}
		if len(output) > maxProcessOutputBytes {
			return nil, errors.New("process output exceeded 1 MiB; outcome remains unknown")
		}
		if result.Exited && len(chunks) == 0 {
			exitCode := result.ExitCode
			if err := e.store.ProcessResult(ctx, e.owner, processID, after, "exited", exitCode, true); err != nil {
				return nil, err
			}
			return processOutput(output, exitCode, nil), nil
		}
		if !result.Exited && !time.Now().Before(deadline) {
			if err := e.store.ProcessResult(ctx, e.owner, processID, after, "running", nil, true); err != nil {
				return nil, err
			}
			return processOutput(output, nil, &processID), nil
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
		if row.Status != "running" {
			lock.Unlock()
			return progressed, fmt.Errorf("cannot reconcile remote process in state %s", row.Status)
		}
		pageCtx, cancel := context.WithTimeout(ctx, timeout)
		result, rpcErr := executorRPC[executorReadResult](pageCtx, e, "process/read", executorRead{ProcessID: row.ID, AfterSeq: row.AfterSequence, MaxBytes: 65536})
		cancel()
		if rpcErr != nil {
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
			if err := e.store.RetainProcessOutput(ctx, e.owner, row.ID, seq, output, result.Exited && len(chunks) == 0, exitCode); err != nil {
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
			if readErr == nil && result.SessionID == nil {
				outcomes[process.ID] = map[string]any{"exit_observed": true, "result": result}
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
