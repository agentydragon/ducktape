package runner

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
	commandpb "github.com/agentydragon/ducktape/agentplane/proto/command"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	controlpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/runner_controlpb"
	"github.com/google/uuid"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"golang.org/x/sync/errgroup"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type Runner struct {
	runnerservicepb.UnimplementedRunnerServer
	config      Config
	ctx         context.Context
	cancel      context.CancelFunc
	owner       *StateOwner
	store       *SessionStore
	bootID      string
	mu          sync.Mutex
	createMu    sync.Mutex
	loadMu      sync.Mutex
	bootstrapMu sync.Mutex
	sessions    map[string]*Session
	codex       *codexServer
	executor    *codexExecutor
	changes     chan struct{}
	fatal       chan error
	idleSince   *timestamppb.Timestamp
	admissions  int
	quiescing   bool
	quiesceDone chan struct{}
	initLog     *InitializationLog
	initDone    chan struct{}
	initErr     error
}

const (
	autosuspendBootstrap = "Bootstrap"
	autosuspendAdmission = "Admission"
)

func New(config Config) (*Runner, error) {
	if config.CodexExecutor != nil {
		if config.Codex == nil {
			return nil, errors.New("Codex executor requires a configured Codex harness")
		}
		if err := config.CodexExecutor.validate(config.ProcessIsolation); err != nil {
			return nil, err
		}
	}
	if _, present := config.HarnessEnvironment["CODEX_EXEC_SERVER_URL"]; present {
		return nil, errors.New("CODEX_EXEC_SERVER_URL is owned by Runner executor configuration")
	}
	owner, err := OpenStateOwner(config.StateDir)
	if err != nil {
		return nil, err
	}
	if config.ProcessIsolation != nil {
		if err := config.ProcessIsolation.Prepare(); err != nil {
			_ = owner.Close()
			return nil, err
		}
	}
	if config.CodexExecutor != nil && config.CodexExecutor.Isolation != nil {
		if err := config.CodexExecutor.Isolation.Prepare(); err != nil {
			_ = owner.Close()
			return nil, err
		}
	}
	if config.CodexExecutor != nil && config.CodexExecutor.ContainerSocket != "" {
		ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
		err := fenceExecutorContainer(ctx, config.CodexExecutor.ContainerSocket, "")
		cancel()
		if err != nil {
			_ = owner.Close()
			return nil, err
		}
	}
	store, err := NewSessionStore(filepath.Join(config.StateDir, "sessions"))
	if err != nil {
		_ = owner.Close()
		return nil, err
	}
	ctx, cancel := context.WithCancel(context.Background())
	r := &Runner{config: config, owner: owner, store: store, bootID: uuid.NewString(), ctx: ctx, cancel: cancel, sessions: map[string]*Session{}, changes: make(chan struct{}), fatal: make(chan error, 1)}
	if config.CodexExecutor != nil {
		r.executor = newCodexExecutor(r, *config.CodexExecutor)
	}
	if config.Codex != nil {
		r.codex = newCodexServer(r, *config.Codex)
	}
	return r, nil
}

func (r *Runner) changed() {
	r.mu.Lock()
	close(r.changes)
	r.changes = make(chan struct{})
	r.mu.Unlock()
}

func (r *Runner) changesSince() <-chan struct{} { r.mu.Lock(); defer r.mu.Unlock(); return r.changes }

func (r *Runner) fail(err error) {
	if err == nil {
		return
	}
	slog.Error("runner operation failed", "error", err)
	select {
	case r.fatal <- err:
	default:
	}
}

func (r *Runner) inventory() []*Session {
	r.mu.Lock()
	defer r.mu.Unlock()
	sessions := make([]*Session, 0, len(r.sessions))
	for _, s := range r.sessions {
		sessions = append(sessions, s)
	}
	sort.Slice(sessions, func(i, j int) bool { return sessions[i].id < sessions[j].id })
	return sessions
}

func (r *Runner) Startup(ctx context.Context) error {
	ids, err := r.store.SessionIDs()
	if err != nil {
		return err
	}
	for _, id := range ids {
		s, err := r.load(ctx, id)
		if err != nil {
			return err
		}
		if err := s.recover(ctx); err != nil {
			return err
		}
		if err := r.resumeRecovered(s); err != nil {
			return err
		}
	}
	if r.config.Launch.Bootstrap != nil {
		if err := r.startBootstrap(); err != nil {
			if status.Code(err) != codes.AlreadyExists {
				return err
			}
			// Keep serving durable reservations; activation reports the conflicting
			// bootstrap without executing it or abandoning admitted commands.
			slog.Error("the Sandbox selected a different bootstrap script", "error", err)
		}
	}
	r.changed()
	return nil
}

func (r *Runner) load(ctx context.Context, id string) (*Session, error) {
	r.loadMu.Lock()
	defer r.loadMu.Unlock()
	r.mu.Lock()
	s := r.sessions[id]
	r.mu.Unlock()
	if s != nil {
		return s, nil
	}
	record, err := r.store.Read(id)
	if err != nil {
		return nil, err
	}
	if _, err := ResolveWorkspace(record.CWD, r.config.WorkspaceRoot); err != nil {
		return nil, err
	}
	directory, err := r.store.Directory(id)
	if err != nil {
		return nil, err
	}
	journal, err := OpenJournal(ctx, filepath.Join(directory, "journal.sqlite"), record.EventSourceID)
	if err != nil {
		return nil, err
	}
	s = newSession(r, id, &record, journal)
	if err := s.load(ctx); err != nil {
		_ = journal.Close()
		return nil, err
	}
	r.mu.Lock()
	r.sessions[id] = s
	r.mu.Unlock()
	s.work.Add(1)
	go s.commandLoop()
	if err := r.reopen(ctx, s); err != nil {
		return nil, err
	}
	r.changed()
	return s, nil
}

func (r *Runner) resumeRecovered(s *Session) error {
	if r.store.Lameduck() || s.journal.SealState() != nil {
		return nil
	}
	commands, err := s.journal.PendingCommands(r.ctx)
	if err != nil {
		return err
	}
	turns, err := s.journal.UnreconciledProcessLostTurns(r.ctx)
	if err != nil {
		return err
	}
	if len(commands) > 0 || len(turns) > 0 {
		s.activate()
		s.wake()
	}
	return nil
}

func (r *Runner) adoptIncarnation(ctx context.Context, uid string) error {
	r.createMu.Lock()
	defer r.createMu.Unlock()
	if err := r.store.AdoptIncarnation(uid); err != nil {
		return err
	}
	for _, s := range r.inventory() {
		if err := r.reopen(ctx, s); err != nil {
			return err
		}
		if err := r.resumeRecovered(s); err != nil {
			return err
		}
	}
	r.changed()
	return nil
}

func (r *Runner) reopen(ctx context.Context, s *Session) error {
	uid, known := r.store.IncarnationUID()
	seal := s.journal.SealState()
	if !known || r.store.Lameduck() || seal == nil || seal.IncarnationUID == uid {
		return nil
	}
	if err := s.journal.Reopen(ctx, uid); err != nil {
		return err
	}
	s.mu.Lock()
	s.sealing = false
	s.tearingDown = false
	s.mu.Unlock()
	ack, err := r.store.ReadAcknowledgement(s.id)
	if err != nil {
		return err
	}
	if ack == nil {
		ack = &IngressAcknowledgement{}
	}
	ack.Sealed = false
	if err := r.store.WriteAcknowledgement(s.id, *ack); err != nil {
		return err
	}
	r.changed()
	return nil
}

func (r *Runner) ensureWorkspace(ctx context.Context, cwd string) (string, error) {
	var uid, gid *uint32
	if c := r.config.ProcessIsolation; c != nil {
		uid = &c.AgentUID
		gid = &c.AgentGID
	}
	return EnsureWorkspace(ctx, cwd, r.config.WorkspaceRoot, uid, gid)
}

func (r *Runner) runScript(ctx context.Context, script, cwd string, output func(bool, []byte) error) (int, error) {
	env := map[string]string{}
	for _, entry := range os.Environ() {
		key, value, ok := strings.Cut(entry, "=")
		if ok {
			env[key] = value
		}
	}
	for key, value := range r.config.HarnessEnvironment {
		env[key] = value
	}
	process, err := NewProcess(ProcessConfig{Command: []string{"/bin/sh", "-eu"}, Dir: cwd, Env: env, StateOwnerFD: r.owner.File(), ProcessIsolation: r.config.ProcessIsolation})
	if err != nil {
		return 0, err
	}
	if err := process.Start(ctx); err != nil {
		return 0, err
	}
	var readers errgroup.Group
	for _, stream := range []struct {
		reader io.Reader
		stdout bool
	}{{process.Stdout(), true}, {process.Stderr(), false}} {
		readers.Go(func() error {
			buffer := make([]byte, 64*1024)
			for {
				n, err := stream.reader.Read(buffer)
				if n > 0 {
					if e := output(stream.stdout, append([]byte(nil), buffer[:n]...)); e != nil {
						return errors.Join(e, process.Stop(context.Background(), stopGrace))
					}
				}
				if err == io.EOF {
					return nil
				}
				if err != nil {
					return errors.Join(err, process.Stop(context.Background(), stopGrace))
				}
			}
		})
	}
	writeErr := process.Write(ctx, []byte(script))
	if errors.Is(writeErr, os.ErrClosed) || errors.Is(writeErr, io.ErrClosedPipe) {
		writeErr = nil
	}
	closeErr := process.CloseStdin()
	code, waitErr := process.Wait(ctx)
	if waitErr != nil {
		waitErr = errors.Join(waitErr, process.Stop(context.WithoutCancel(ctx), 5*time.Second))
	}
	return code, errors.Join(writeErr, closeErr, waitErr, readers.Wait())
}

func (r *Runner) startBootstrap() error {
	r.bootstrapMu.Lock()
	defer r.bootstrapMu.Unlock()
	if r.initDone != nil {
		return nil
	}
	script := r.config.Launch.Bootstrap
	if script == nil {
		return nil
	}
	if len(*script) == 0 || len(*script) > 65536 {
		return errors.New("bootstrap must contain 1..65536 UTF-8 bytes")
	}
	root := filepath.Join(r.config.StateDir, "initialization")
	if err := makeDirectory(root); err != nil {
		return err
	}
	digest := sha256.Sum256([]byte(*script))
	metadata := struct {
		SHA string `json:"script_sha256"`
	}{hex.EncodeToString(digest[:])}
	path := filepath.Join(root, "request.json")
	raw, err := os.ReadFile(path)
	selected := errors.Is(err, os.ErrNotExist)
	if err != nil && !selected {
		return err
	}
	if !selected {
		var previous struct {
			SHA string `json:"script_sha256"`
		}
		if err := DecodeStrict(raw, &previous); err != nil {
			return err
		}
		if previous.SHA != metadata.SHA {
			return status.Error(codes.AlreadyExists, "sandbox already selected a different bootstrap script")
		}
	} else {
		encoded, err := json.Marshal(metadata)
		if err != nil {
			return err
		}
		if err := writeDurable(path, encoded); err != nil {
			return err
		}
	}
	log, err := OpenInitializationLog(filepath.Join(root, "events.jsonl"))
	if err != nil {
		return err
	}
	r.initLog = log
	r.initDone = make(chan struct{})
	if log.Ended() {
		close(r.initDone)
		return nil
	}
	if !selected {
		if err := log.AppendInterrupted(1); err != nil {
			return err
		}
		close(r.initDone)
		return nil
	}
	go func() {
		defer close(r.initDone)
		defer r.changed()
		cwd := r.config.InitializationCwd
		if cwd == "" {
			cwd = r.config.StateDir
		}
		code, err := r.runScript(r.ctx, *script, cwd, func(stdout bool, data []byte) error {
			stream := runnerpb.InitializationStream_INITIALIZATION_STREAM_STDERR
			if stdout {
				stream = runnerpb.InitializationStream_INITIALIZATION_STREAM_STDOUT
			}
			return log.AppendOutput(1, stream, data)
		})
		if err == nil {
			_, err = log.AppendResult(1, int32(code))
		}
		r.bootstrapMu.Lock()
		r.initErr = err
		r.bootstrapMu.Unlock()
		if err != nil {
			r.fail(err)
		}
	}()
	r.changed()
	return nil
}

func (r *Runner) bootstrap(ctx context.Context) error {
	if r.config.Launch.Bootstrap == nil {
		return nil
	}
	if err := r.startBootstrap(); err != nil {
		return err
	}
	r.bootstrapMu.Lock()
	done := r.initDone
	r.bootstrapMu.Unlock()
	select {
	case <-done:
	case <-ctx.Done():
		return ctx.Err()
	}
	r.bootstrapMu.Lock()
	defer r.bootstrapMu.Unlock()
	if r.initErr != nil {
		return r.initErr
	}
	events := r.initLog.Since(r.initLog.LastSequence() - 1)
	if len(events) != 1 {
		return errors.New("bootstrap has no terminal outcome")
	}
	last := events[0]
	if last.GetInterrupted() != nil {
		return errors.New("the Sandbox bootstrap was interrupted and never runs again; use a new Sandbox")
	}
	if last.GetResult() == nil || last.GetResult().ExitCode != 0 {
		return errors.New("the Sandbox bootstrap failed and never runs again; use a new Sandbox")
	}
	return nil
}

func writeDurable(path string, data []byte) error {
	file, err := os.OpenFile(path+".tmp", os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	_, writeErr := file.Write(data)
	syncErr := file.Sync()
	closeErr := file.Close()
	if err := errors.Join(writeErr, syncErr, closeErr); err != nil {
		return err
	}
	if err := os.Rename(path+".tmp", path); err != nil {
		return err
	}
	return syncDirectory(filepath.Dir(path))
}

func (r *Runner) OpenSession(ctx context.Context, request *runnerservicepb.OpenSessionRequest) (response *runnerservicepb.OpenSessionResponse, resultErr error) {
	ctx, span := observability.Start(
		ctx, "Runner OpenSession",
		trace.WithAttributes(
			attribute.String("session.id", request.GetSessionId()),
			attribute.String("sandbox.name", r.config.Launch.Sandbox.Name),
		),
	)
	defer func() { observability.End(span, resultErr) }()
	if request.Spec == nil {
		return nil, status.Error(codes.InvalidArgument, "spec is required")
	}
	if _, err := ValidateSessionID(request.SessionId); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	if r.store.Lameduck() {
		return nil, status.Error(codes.Aborted, "this Sandbox is quiesced")
	}
	spec := proto.Clone(request.Spec).(*runnerpb.SessionSpec)
	spec.Instructions = r.config.Launch.Instructions(request.SessionId, spec.Instructions)
	if spec.Harness != runnerpb.Harness_HARNESS_CLAUDE && spec.Harness != runnerpb.Harness_HARNESS_CODEX {
		return nil, status.Error(codes.InvalidArgument, "spec.harness must be CLAUDE or CODEX")
	}
	if !filepath.IsAbs(spec.Cwd) || spec.Model == "" {
		return nil, status.Error(codes.InvalidArgument, "spec.cwd must be absolute and spec.model is required")
	}
	if _, err := ResolveWorkspace(spec.Cwd, r.config.WorkspaceRoot); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	if len(request.GetSetupScript()) > 65536 {
		return nil, status.Error(codes.InvalidArgument, "setup_script exceeds 65536 UTF-8 bytes")
	}
	// Serialize reservation identity, but never hold the quiesce barrier across the
	// HTTP model lookup: quiesce can reject a late creation before its durable write.
	r.createMu.Lock()
	defer r.createMu.Unlock()
	exists, err := r.store.Exists(request.SessionId)
	if err != nil {
		return nil, rpcError(err)
	}
	if exists {
		s, err := r.load(r.ctx, request.SessionId)
		if err != nil {
			return nil, rpcError(err)
		}
		if !proto.Equal(s.attached().Spec, spec) {
			return nil, status.Errorf(codes.AlreadyExists, "session %s exists with a different spec", request.GetSessionId())
		}
		if request.SetupScript != nil {
			hash := sha256.Sum256([]byte(*request.SetupScript))
			var selected *string
			if *request.SetupScript != "" {
				value := hex.EncodeToString(hash[:])
				selected = &value
			}
			if !equalString(s.record.SetupScriptSHA256, selected) {
				return nil, status.Error(codes.AlreadyExists, "session exists with a different setup script")
			}
		}
		return &runnerservicepb.OpenSessionResponse{Attached: s.attached()}, nil
	}
	budget, err := r.config.ResolveModelConfig(ctx, spec.Harness, spec.Model)
	if err != nil {
		return nil, status.Error(codes.FailedPrecondition, err.Error())
	}
	record, err := NewSessionRecord(spec, request.GetSetupScript(), budget)
	if err != nil {
		return nil, rpcError(err)
	}
	r.mu.Lock()
	if r.store.Lameduck() {
		r.mu.Unlock()
		return nil, status.Error(codes.Aborted, "this Sandbox is quiesced")
	}
	err = r.store.Write(request.SessionId, record)
	r.mu.Unlock()
	if err != nil {
		return nil, rpcError(err)
	}
	s, err := r.load(r.ctx, request.SessionId)
	if err != nil {
		return nil, rpcError(err)
	}
	if record.SetupScriptSHA256 != nil {
		r.mu.Lock()
		if r.store.Lameduck() {
			r.mu.Unlock()
			return nil, status.Error(codes.Aborted, "this Sandbox is quiesced")
		}
		s.startSetup(request.GetSetupScript())
		r.mu.Unlock()
	}
	r.changed()
	return &runnerservicepb.OpenSessionResponse{Attached: s.attached()}, nil
}

func equalString(a, b *string) bool { return a == nil && b == nil || a != nil && b != nil && *a == *b }

func (r *Runner) admit(id string, c *commandpb.Command) (*eventlogpb.EventEntry, error) {
	if c.CommandId == "" || c.Operation == nil {
		return nil, status.Error(codes.InvalidArgument, "command_id and operation required")
	}
	if v := c.GetInterruptTurn(); v != nil && v.TurnId == "" {
		return nil, status.Error(codes.InvalidArgument, "interrupt_turn.turn_id is required")
	}
	r.mu.Lock()
	lameduck := r.store.Lameduck()
	r.admissions++
	r.idleSince = nil
	r.mu.Unlock()
	r.changed()
	defer func() { r.mu.Lock(); r.admissions--; r.mu.Unlock(); r.changed() }()
	exists, err := r.store.Exists(id)
	if err != nil {
		return nil, rpcError(err)
	}
	if !exists {
		return nil, status.Error(codes.NotFound, "no such session")
	}
	s, err := r.load(r.ctx, id)
	if err != nil {
		return nil, rpcError(err)
	}
	entry, err := s.admit(r.ctx, c, lameduck)
	return entry, rpcError(err)
}

func (r *Runner) SubmitInput(_ context.Context, q *runnerservicepb.SubmitInputRequest) (*runnerservicepb.SubmitInputResponse, error) {
	entry, err := r.admit(q.SessionId, &commandpb.Command{CommandId: q.CommandId, Operation: &commandpb.Command_SubmitInput{SubmitInput: &commandpb.SubmitInput{Text: q.Text}}})
	return &runnerservicepb.SubmitInputResponse{Admission: entry}, err
}

func (r *Runner) ChangeModel(_ context.Context, q *runnerservicepb.ChangeModelRequest) (*runnerservicepb.ChangeModelResponse, error) {
	entry, err := r.admit(q.SessionId, &commandpb.Command{CommandId: q.CommandId, Operation: &commandpb.Command_ChangeModel{ChangeModel: &commandpb.ChangeModel{Model: q.Model}}})
	return &runnerservicepb.ChangeModelResponse{Admission: entry}, err
}

func (r *Runner) ChangeReasoningEffort(_ context.Context, q *runnerservicepb.ChangeReasoningEffortRequest) (*runnerservicepb.ChangeReasoningEffortResponse, error) {
	entry, err := r.admit(q.SessionId, &commandpb.Command{CommandId: q.CommandId, Operation: &commandpb.Command_ChangeReasoningEffort{ChangeReasoningEffort: &commandpb.ChangeReasoningEffort{Effort: q.Effort}}})
	return &runnerservicepb.ChangeReasoningEffortResponse{Admission: entry}, err
}

func (r *Runner) InterruptTurn(_ context.Context, q *runnerservicepb.InterruptTurnRequest) (*runnerservicepb.InterruptTurnResponse, error) {
	entry, err := r.admit(q.SessionId, &commandpb.Command{CommandId: q.CommandId, Operation: &commandpb.Command_InterruptTurn{InterruptTurn: &commandpb.InterruptTurn{TurnId: q.TurnId}}})
	return &runnerservicepb.InterruptTurnResponse{Admission: entry}, err
}

func rpcError(err error) error {
	if err == nil {
		return nil
	}
	if _, ok := status.FromError(err); ok {
		return err
	}
	switch {
	case errors.Is(err, ErrCommandConflict):
		return status.Error(codes.AlreadyExists, err.Error())
	case errors.Is(err, ErrActiveTurnChanged):
		return status.Error(codes.FailedPrecondition, err.Error())
	case errors.Is(err, ErrJournalSealed):
		return status.Error(codes.FailedPrecondition, err.Error())
	}
	return status.Error(codes.Unavailable, err.Error())
}

func (r *Runner) ListSessions(ctx context.Context, _ *runnerpb.ListSessionsRequest) (*runnerpb.ListSessionsResponse, error) {
	r.mu.Lock()
	draining := r.quiesceDone
	r.mu.Unlock()
	if draining != nil {
		select {
		case <-draining:
		case <-ctx.Done():
			return nil, status.FromContextError(ctx.Err()).Err()
		}
	}
	result := &runnerpb.ListSessionsResponse{}
	for _, s := range r.inventory() {
		a := s.attached()
		result.Sessions = append(result.Sessions, &runnerpb.SessionSummary{SessionId: a.SessionId, Spec: a.Spec, LastCursor: a.LastCursor, HarnessState: a.HarnessState, ActiveTurnId: a.ActiveTurnId, SetupState: a.SetupState, LaunchOutcome: a.LaunchOutcome})
	}
	return result, nil
}

func (r *Runner) GetStatus(_ context.Context, _ *runnerpb.GetStatusRequest) (*runnerpb.RunnerStatus, error) {
	result := &runnerpb.RunnerStatus{RunnerBootId: r.bootID}
	for _, s := range r.inventory() {
		s.mu.Lock()
		execution := runnerpb.SessionExecutionState_SESSION_EXECUTION_STATE_DORMANT
		if s.ready {
			execution = runnerpb.SessionExecutionState_SESSION_EXECUTION_STATE_READY
		}
		if !s.ready && (s.activating || s.processID != "" || len(s.pending) > 0) || s.setup == runnerpb.SetupState_SETUP_STATE_RUNNING {
			execution = runnerpb.SessionExecutionState_SESSION_EXECUTION_STATE_PREPARING
		}
		if s.failure != nil {
			execution = runnerpb.SessionExecutionState_SESSION_EXECUTION_STATE_FAILED
		}
		row := &runnerpb.SessionStatus{SessionId: s.id, ExecutionState: execution, NativeSessionId: s.record.NativeSessionID, FailureCursor: s.failure, Busy: s.activeTurn != "" || len(s.pending) > 0 || s.activating || s.setup == runnerpb.SetupState_SETUP_STATE_RUNNING}
		if s.activeTurn != "" {
			value := s.activeTurn
			row.ActiveTurnId = &value
		}
		if s.processID != "" {
			id := s.processID
			row.ProcessId = &id
		}
		if s.processID != "" && s.codex == nil {
			process := &runnerpb.HarnessProcessStatus{ProcessId: s.processID, Harness: s.record.Harness, State: s.processState, AssignedSessionIds: []string{s.id}}
			if s.process != nil && s.process.Running() {
				pid := int64(s.process.PID())
				process.Pid = &pid
			}
			result.Processes = append(result.Processes, process)
		}
		result.Sessions = append(result.Sessions, row)
		s.mu.Unlock()
	}
	if r.codex != nil {
		if process := r.codex.status(); process != nil {
			result.Processes = append(result.Processes, process)
		}
	}
	return result, nil
}

func (r *Runner) facts() (*controlpb.RunnerFacts, error) {
	sessions := r.inventory()
	var blockers []*sandboxpb.AutosuspendBlocker
	setup := false
	for _, s := range sessions {
		sessionBlockers := s.autosuspendBlockers()
		for _, blocker := range sessionBlockers {
			setup = setup || blocker.GetReason() == autosuspendSetup
		}
		blockers = append(blockers, sessionBlockers...)
	}
	r.bootstrapMu.Lock()
	initLog := r.initLog
	r.bootstrapMu.Unlock()
	facts := &controlpb.RunnerFacts{SetupsRunning: setup}
	if initLog != nil {
		facts.Bootstrap = controlpb.RunnerFacts_BOOTSTRAP_RUNNING
		if initLog.Ended() {
			last := initLog.Since(initLog.LastSequence() - 1)[0]
			facts.Bootstrap = controlpb.RunnerFacts_BOOTSTRAP_FAILED
			if last.GetInterrupted() != nil {
				facts.Bootstrap = controlpb.RunnerFacts_BOOTSTRAP_INTERRUPTED
			} else if last.GetResult() != nil && last.GetResult().ExitCode == 0 {
				facts.Bootstrap = controlpb.RunnerFacts_BOOTSTRAP_SUCCEEDED
			}
		} else {
			blockers = append(blockers, &sandboxpb.AutosuspendBlocker{Reason: autosuspendBootstrap})
		}
	}
	r.mu.Lock()
	if r.admissions > 0 {
		blockers = append(blockers, &sandboxpb.AutosuspendBlocker{Reason: autosuspendAdmission})
	}
	busy := len(blockers) > 0
	if busy {
		r.idleSince = nil
	} else if r.idleSince == nil {
		r.idleSince = timestamppb.Now()
	}
	if r.idleSince != nil {
		facts.IdleSince = proto.Clone(r.idleSince).(*timestamppb.Timestamp)
	}
	quiescing := r.quiescing
	r.mu.Unlock()
	sort.Slice(blockers, func(i, j int) bool {
		leftSession, rightSession := blockers[i].SessionId, blockers[j].SessionId
		if (leftSession != nil) != (rightSession != nil) {
			return leftSession != nil
		}
		if leftSession != nil && *leftSession != *rightSession {
			return *leftSession < *rightSession
		}
		return blockers[i].Reason < blockers[j].Reason
	})
	facts.AutosuspendBlockers = blockers
	if r.store.Lameduck() {
		facts.QuiesceStage = controlpb.RunnerFacts_QUIESCE_STAGE_QUIESCED
		flushed := true
		for _, s := range sessions {
			seal := s.journal.SealState()
			if seal == nil {
				quiescing = true
				continue
			}
			ack, err := r.store.ReadAcknowledgement(s.id)
			if err != nil {
				return nil, err
			}
			flushed = flushed && ack != nil && ack.Sealed && ack.Cursor == seal.Cursor
		}
		if !flushed {
			facts.QuiesceStage = controlpb.RunnerFacts_QUIESCE_STAGE_FLUSHING
		}
		if quiescing {
			facts.QuiesceStage = controlpb.RunnerFacts_QUIESCE_STAGE_DRAINING
		}
	}
	return facts, nil
}

func (r *Runner) Quiesce(_ context.Context, q *runnerpb.QuiesceRequest) (*runnerpb.QuiesceResponse, error) {
	_, err := r.facts()
	if err != nil {
		return nil, rpcError(err)
	}
	r.mu.Lock()
	if !r.store.Lameduck() && q.IfIdleSince != nil && (r.idleSince == nil || r.idleSince.AsTime().UnixMicro() != q.IfIdleSince.AsTime().UnixMicro()) {
		r.mu.Unlock()
		return nil, status.Error(codes.Aborted, "the Sandbox is busy or its idle_since differs")
	}
	if err := r.store.EnterLameduck(); err != nil {
		r.mu.Unlock()
		return nil, rpcError(err)
	}
	if !r.quiescing && r.quiesceDone == nil {
		r.quiescing = true
		r.quiesceDone = make(chan struct{})
		go r.drain()
	}
	r.mu.Unlock()
	r.changed()
	facts, err := r.facts()
	if err != nil {
		return nil, rpcError(err)
	}
	return &runnerpb.QuiesceResponse{Stage: runnerpb.QuiesceStage(facts.QuiesceStage)}, nil
}

func (r *Runner) drain() {
	var drainErr error
	defer func() {
		r.mu.Lock()
		done := r.quiesceDone
		r.quiescing = false
		close(done)
		if drainErr != nil {
			r.quiesceDone = nil
		}
		r.mu.Unlock()
		r.changed()
		if drainErr != nil {
			slog.Error("quiesce drain failed; next Quiesce may retry", "error", drainErr)
		}
	}()
	for {
		r.mu.Lock()
		n := r.admissions
		changed := r.changes
		r.mu.Unlock()
		if n == 0 {
			break
		}
		select {
		case <-changed:
		case <-r.ctx.Done():
			drainErr = r.ctx.Err()
			return
		}
	}
	ids, err := r.store.SessionIDs()
	if err != nil {
		drainErr = err
		return
	}
	sessions := make([]*Session, 0, len(ids))
	for _, id := range ids {
		s, err := r.load(r.ctx, id)
		if err != nil {
			drainErr = err
			return
		}
		sessions = append(sessions, s)
	}
	if err := r.stopSessions(r.ctx, sessions, true); err != nil {
		drainErr = err
		return
	}
	r.bootstrapMu.Lock()
	done := r.initDone
	r.bootstrapMu.Unlock()
	if done != nil {
		<-done
	}
}

// stopSessions halts every Session in `sessions`, ordered by ID, then stops their harness processes, then
// seals their journals when `seal` is set. The shared Codex app-server stops once, after all of them halted.
func (r *Runner) stopSessions(ctx context.Context, sessions []*Session, seal bool) error {
	for _, s := range sessions {
		s.shutdownMu.Lock()
		defer s.shutdownMu.Unlock()
	}
	for _, s := range sessions {
		s.halt(ctx, seal)
	}
	var errs []error
	for _, s := range sessions {
		errs = append(errs, s.stopProcess(ctx))
	}
	if r.codex != nil {
		errs = append(errs, r.codex.stop(ctx))
	}
	if r.executor != nil {
		errs = append(errs, r.executor.stop(ctx))
	}
	for _, s := range sessions {
		errs = append(errs, s.reconcilePending(ctx))
	}
	if err := errors.Join(errs...); err != nil || !seal {
		return err
	}
	for _, s := range sessions {
		if err := s.sealJournal(ctx); err != nil {
			return err
		}
	}
	return nil
}

func (r *Runner) Close() error {
	// Streams remain available while orderly shutdown settles native processes and
	// one-shot scripts. The ownership descriptor is unlocked only after all cleanup.
	errs := []error{r.stopSessions(r.ctx, r.inventory(), false)}
	r.bootstrapMu.Lock()
	initDone := r.initDone
	r.bootstrapMu.Unlock()
	if initDone != nil {
		<-initDone
	}
	r.cancel()
	for _, s := range r.inventory() {
		s.work.Wait()
		errs = append(errs, s.journal.Close())
	}
	if r.initLog != nil {
		errs = append(errs, r.initLog.Close())
	}
	if err := errors.Join(errs...); err != nil {
		return err
	}
	return r.owner.Close()
}
