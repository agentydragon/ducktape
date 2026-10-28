package runner

import (
	"context"
	"errors"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"sync"
	"time"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/google/uuid"
	"go.uber.org/zap"
	"golang.org/x/sync/errgroup"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type Runner struct {
	runnerservicepb.UnimplementedRunnerServer
	config              Config
	ctx                 context.Context
	cancel              context.CancelFunc
	owner               *StateOwner
	supervisor          *supervision.Supervisor
	executorSupervisor  *supervision.Supervisor
	workspaceSupervisor *supervision.Supervisor
	store               *SessionStore
	bootID              string
	mu                  sync.Mutex
	createMu            sync.Mutex
	loadMu              sync.Mutex
	bootstrapMu         sync.Mutex
	sessions            map[string]*Session
	codexAppServer      *codexAppServer
	codexExecServer     *codexExecServer
	changes             chan struct{}
	fatal               chan error
	idleSince           *timestamppb.Timestamp
	admissions          int
	lifecycle           *lifecycle
	closeOnce           sync.Once
	closeErr            error
	initLog             *InitializationLog
	bootstrapOp         *operation
}

func New(config Config) (*Runner, error) {
	if err := config.resolveSessionReconcileConcurrency(); err != nil {
		return nil, err
	}
	if config.CodexExecServer != nil {
		if config.Codex == nil {
			return nil, errors.New("Codex exec-server requires a configured Codex harness")
		}
		if err := config.CodexExecServer.validate(config.VMProcessPolicy); err != nil {
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
	supervisor := supervision.NewSubprocessSupervisor(owner.File())
	var workspaceSupervisor, executorSupervisor *supervision.Supervisor
	if config.VMProcessPolicy != nil {
		supervisor, err = config.VMProcessPolicy.Prepare(config.StateDir, config.Launch.Sandbox.UID)
		if err != nil {
			_ = owner.Close()
			return nil, err
		}
		workspaceSupervisor = supervisor
	}
	if config.CodexExecServer != nil && config.CodexExecServer.Isolation != nil {
		executorSupervisor, err = config.CodexExecServer.Isolation.Prepare(config.StateDir, config.Launch.Sandbox.UID)
		if err != nil {
			_ = owner.Close()
			return nil, err
		}
	}
	if config.CodexExecServer != nil && config.CodexExecServer.ContainerSocket != "" {
		ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
		err := fenceCodexExecServerContainer(ctx, config.CodexExecServer.ContainerSocket, "")
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
	r := &Runner{config: config, owner: owner, supervisor: supervisor, workspaceSupervisor: workspaceSupervisor, executorSupervisor: executorSupervisor, store: store, bootID: uuid.NewString(), ctx: ctx, cancel: cancel, sessions: map[string]*Session{}, changes: make(chan struct{}), fatal: make(chan error, 1)}
	if config.CodexExecServer != nil {
		r.codexExecServer = newCodexExecServer(r, *config.CodexExecServer)
	}
	if config.Codex != nil {
		r.codexAppServer = newCodexAppServer(r, *config.Codex)
	}
	lifecycle, err := newLifecycle(r)
	if err != nil {
		cancel()
		_ = owner.Close()
		return nil, err
	}
	r.lifecycle = lifecycle
	go lifecycle.run()
	return r, nil
}

func (r *Runner) changed() {
	r.mu.Lock()
	close(r.changes)
	r.changes = make(chan struct{})
	r.mu.Unlock()
	if r.lifecycle != nil {
		r.lifecycle.touchRunner()
	}
}

func (r *Runner) changesSince() <-chan struct{} { r.mu.Lock(); defer r.mu.Unlock(); return r.changes }

func (r *Runner) fail(err error) {
	if err == nil {
		return
	}
	zap.L().Error("runner operation failed", zap.Error(err))
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
		if err := r.requestBootstrap(); err != nil {
			return err
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
	if err := r.lifecycle.publishSeal(s); err != nil {
		return nil, err
	}
	ack, err := r.store.ReadAcknowledgement(id)
	if err != nil {
		return nil, err
	}
	if ack != nil {
		if err := r.lifecycle.publishAck(id, *ack); err != nil {
			return nil, err
		}
	}
	r.lifecycle.publishSession(s, false)
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
		s.requestExecution()
	}
	return nil
}

func (r *Runner) adoptIncarnation(ctx context.Context, uid string) error {
	r.createMu.Lock()
	defer r.createMu.Unlock()
	// A replacement incarnation can reconnect while the old one is still
	// draining. Finish its native effects and seals before resetting demand;
	// ingress acknowledgement belongs to the old incarnation and need not wait.
	for {
		r.mu.Lock()
		desiredState := r.lifecycle.desiredState()
		if desiredState.Mode == runnerClosing {
			r.mu.Unlock()
			return errors.New("runner is closing")
		}
		previous, known := r.store.IncarnationUID()
		progress := r.lifecycle.status()
		if desiredState.Mode != runnerQuiescing || !known || previous == uid ||
			(progress.Generation == desiredState.Generation && progress.Stage >= stageFlushing && progress.Error == "") {
			break
		}
		r.mu.Unlock()
		if err := r.lifecycle.wait(ctx, stageFlushing); err != nil {
			return err
		}
	}
	if err := r.store.AdoptIncarnation(uid); err != nil {
		r.mu.Unlock()
		return err
	}
	mode, sealUID := runnerRunning, ""
	if r.store.Lameduck() {
		mode, sealUID = runnerQuiescing, uid
	}
	err := r.lifecycle.setDesiredState(mode, sealUID)
	r.mu.Unlock()
	if err != nil {
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
	if err := r.lifecycle.publishAck(s.id, *ack); err != nil {
		return err
	}
	if err := r.lifecycle.publishSeal(s); err != nil {
		return err
	}
	r.changed()
	return nil
}

func (r *Runner) ensureWorkspace(ctx context.Context, cwd string) (string, error) {
	return EnsureWorkspace(ctx, cwd, r.config.WorkspaceRoot, r.workspaceSupervisor)
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
	process, err := r.supervisor.NewProcess(supervision.ProcessConfig{Command: []string{"/bin/sh", "-eu"}, Dir: cwd, Env: env})
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

func (r *Runner) Quiesce(_ context.Context, q *runnerpb.QuiesceRequest) (*runnerpb.QuiesceResponse, error) {
	_, err := r.facts()
	if err != nil {
		return nil, rpcError(err)
	}
	r.mu.Lock()
	desiredState, progress := r.lifecycle.desiredState(), r.lifecycle.status()
	if desiredState.Mode == runnerClosing && progress.Generation == desiredState.Generation && progress.Stage == stageClosed {
		r.mu.Unlock()
		return nil, status.Error(codes.Unavailable, "runner is closed")
	}
	if !r.store.Lameduck() && q.IfIdleSince != nil && (r.idleSince == nil || r.idleSince.AsTime().UnixMicro() != q.IfIdleSince.AsTime().UnixMicro()) {
		r.mu.Unlock()
		return nil, status.Error(codes.Aborted, "the Sandbox is busy or its idle_since differs")
	}
	if err := r.store.EnterLameduck(); err != nil {
		r.mu.Unlock()
		return nil, rpcError(err)
	}
	uid, err := r.store.RequireIncarnationUID()
	if err == nil {
		mode := runnerQuiescing
		if r.lifecycle.desiredState().Mode == runnerClosing {
			mode = runnerClosing
		}
		err = r.lifecycle.setDesiredState(mode, uid)
	}
	if err != nil {
		r.mu.Unlock()
		return nil, rpcError(err)
	}
	r.mu.Unlock()
	r.changed()
	facts, err := r.facts()
	if err != nil {
		return nil, rpcError(err)
	}
	return &runnerpb.QuiesceResponse{Stage: runnerpb.QuiesceStage(facts.QuiesceStage)}, nil
}

func (r *Runner) Close() error {
	r.closeOnce.Do(func() {
		r.mu.Lock()
		err := r.lifecycle.setDesiredState(runnerClosing, "")
		r.mu.Unlock()
		r.changed()
		if err == nil {
			err = r.lifecycle.wait(r.ctx, stageClosed)
		}
		if err != nil {
			r.closeErr = err
			return
		}
		// Controllers stop before joining workers, so no new operation can race Wait.
		r.cancel()
		<-r.lifecycle.done
		r.lifecycle.workers.Wait()
		var errs []error
		for _, s := range r.inventory() {
			s.work.Wait()
			errs = append(errs, s.journal.Close())
		}
		if r.initLog != nil {
			errs = append(errs, r.initLog.Close())
		}
		if err := errors.Join(errs...); err != nil {
			r.closeErr = err
			return
		}
		r.closeErr = r.owner.Close()
	})
	return r.closeErr
}
