package runner

import (
	"context"
	"errors"
	"maps"
	"path/filepath"
	"sync"
	"time"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/bootstrap"
	"github.com/agentydragon/ducktape/agentplane/runner/codex/execserver"
	"github.com/agentydragon/ducktape/agentplane/runner/codexappserver"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/agentydragon/ducktape/agentplane/runner/retained"
	runnerservicepb "github.com/agentydragon/ducktape/agentplane/runner/runner_servicepb"
	"github.com/agentydragon/ducktape/agentplane/runner/scripts"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/agentydragon/ducktape/agentplane/runner/workspace"
	"github.com/google/uuid"
	"go.uber.org/zap"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type Runner struct {
	runnerservicepb.UnimplementedRunnerServer
	config              Config
	ctx                 context.Context
	cancel              context.CancelFunc
	owner               *retained.Owner
	supervisor          *supervision.Supervisor
	scripts             *scripts.Runner
	workspaceSupervisor *supervision.Supervisor
	store               *retained.Store
	bootID              string
	mu                  sync.Mutex
	sessions            *sessionManager
	bootstrapHost       *bootstrap.Host
	codexAppServer      *codexappserver.Host
	codexExecServer     *execserver.Host
	changes             chan struct{}
	fatal               chan error
	idleSince           *timestamppb.Timestamp
	admissions          int
	lifecycle           *lifecycle
	closeOnce           sync.Once
	closeErr            error
}

func New(config Config) (*Runner, error) {
	return newRunner(config, nil)
}

func newRunner(config Config, adapters *execserver.Adapters) (*Runner, error) {
	if err := config.resolveSessionReconcileConcurrency(); err != nil {
		return nil, err
	}
	if config.CodexExecServer != nil {
		if config.Codex == nil {
			return nil, errors.New("Codex exec-server requires a configured Codex harness")
		}
		if err := config.CodexExecServer.Validate(config.VMProcessPolicy); err != nil {
			return nil, err
		}
		if adapters != nil && (adapters.Start == nil || adapters.Probe == nil) {
			return nil, errors.New("Codex exec-server test adapters are incomplete")
		}
	} else if adapters != nil {
		return nil, errors.New("Codex exec-server adapters require executor configuration")
	}
	if _, present := config.HarnessEnvironment["CODEX_EXEC_SERVER_URL"]; present {
		return nil, errors.New("CODEX_EXEC_SERVER_URL is owned by Runner executor configuration")
	}
	owner, err := retained.OpenOwner(config.StateDir)
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
	if config.CodexExecServer != nil && adapters == nil {
		adapters = &execserver.Adapters{
			Start: execserver.NativeStart(execserver.NativeConfig{
				Config: *config.CodexExecServer, Binary: config.Codex.Binary, StateDir: config.StateDir,
				Environment: config.HarnessEnvironment, Supervisor: executorSupervisor,
			}),
			Probe: execserver.ProbeAt(config.CodexExecServer.URL),
		}
	}
	if config.CodexExecServer != nil && config.CodexExecServer.ContainerSocket != "" {
		ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
		err := execserver.FenceContainer(ctx, config.CodexExecServer.ContainerSocket, "")
		cancel()
		if err != nil {
			_ = owner.Close()
			return nil, err
		}
	}
	store, err := retained.NewStore(filepath.Join(config.StateDir, "sessions"))
	if err != nil {
		_ = owner.Close()
		return nil, err
	}
	ctx, cancel := context.WithCancel(context.Background())
	r := &Runner{config: config, owner: owner, supervisor: supervisor, workspaceSupervisor: workspaceSupervisor, store: store, bootID: uuid.NewString(), ctx: ctx, cancel: cancel, changes: make(chan struct{}), fatal: make(chan error, 1)}
	r.scripts, err = scripts.New(scripts.Config{
		HarnessEnvironment: config.HarnessEnvironment,
		LaunchProcess: func(ctx context.Context, owner string, processConfig supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
			return r.launchProcess(ctx, owner, r.supervisor, processConfig)
		},
	})
	if err != nil {
		cancel()
		_ = owner.Close()
		return nil, err
	}
	lifecycle, err := newLifecycle(r, adapters)
	if err != nil {
		cancel()
		_ = owner.Close()
		return nil, err
	}
	r.lifecycle = lifecycle
	r.sessions = newSessionManager(r)
	go lifecycle.run()
	return r, nil
}

func newSessionManager(r *Runner) *sessionManager {
	config, store, lifecycle := r.config, r.store, r.lifecycle
	if config.Claude != nil {
		claude := *config.Claude
		config.Claude = &claude
	}
	if config.DebugCheckpoint != nil {
		checkpoint := *config.DebugCheckpoint
		config.DebugCheckpoint = &checkpoint
	}
	return &sessionManager{
		ctx: r.ctx, store: store, workspaceRoot: config.WorkspaceRoot,
		destination: config.Launch.Destination,
		dependencies: sessionDependencies{
			ctx: r.ctx, state: lifecycle.state, store: store,
			claude: config.Claude, harnessEnvironment: maps.Clone(config.HarnessEnvironment),
			debugCheckpoint: config.DebugCheckpoint, scripts: r.scripts, codexAppServer: r.codexAppServer,
			bootstrapHost: r.bootstrapHost, ensureWorkspace: r.ensureWorkspace,
			launchProcess: func(ctx context.Context, owner string, processConfig supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
				return r.launchProcess(ctx, owner, r.supervisor, processConfig)
			},
			resolveModelConfig: config.ResolveModelConfig,
			publish:            lifecycle.publishSession,
			publishOperation: func(id string, op *operation, done bool) {
				lifecycle.operationResult(id, sessionControllerName, op, done)
			},
			changed: r.changed, fail: r.fail,
		},
		publishSeal: lifecycle.publishSeal, publishAck: lifecycle.publishAck,
		publish: lifecycle.publishSession, changed: r.changed,
		sessions: map[string]*Session{},
	}
}

func (r *Runner) changed() {
	r.mu.Lock()
	close(r.changes)
	r.changes = make(chan struct{})
	r.mu.Unlock()
}

func (r *Runner) admissionChanged() {
	r.changed()
	if r.lifecycle != nil {
		r.lifecycle.publishAdmissionState()
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

func (r *Runner) Startup(ctx context.Context) error {
	if err := r.sessions.startup(ctx); err != nil {
		return err
	}
	if r.bootstrapHost.Configured() {
		if err := r.bootstrapHost.Request(ctx); err != nil {
			return err
		}
	}
	r.changed()
	return nil
}

func (r *Runner) adoptIncarnation(ctx context.Context, uid string) error {
	r.sessions.createMu.Lock()
	defer r.sessions.createMu.Unlock()
	// A replacement incarnation can reconnect while the old one is still
	// draining. Finish its native effects and seals before resetting demand;
	// ingress acknowledgement belongs to the old incarnation and need not wait.
	for {
		r.mu.Lock()
		desiredState := r.lifecycle.desiredState()
		if desiredState.Mode == runnerlifecycle.Closing {
			r.mu.Unlock()
			return errors.New("runner is closing")
		}
		previous, known := r.store.IncarnationUID()
		progress := r.lifecycle.status()
		if desiredState.Mode != runnerlifecycle.Quiescing || !known || previous == uid ||
			(progress.Generation == desiredState.Generation && progress.Stage >= runnerlifecycle.Flushing && progress.Error == "") {
			break
		}
		r.mu.Unlock()
		if err := r.lifecycle.wait(ctx, runnerlifecycle.Flushing); err != nil {
			return err
		}
	}
	if err := r.store.AdoptIncarnation(uid); err != nil {
		r.mu.Unlock()
		return err
	}
	mode, sealUID := runnerlifecycle.Running, ""
	if r.store.Lameduck() {
		mode, sealUID = runnerlifecycle.Quiescing, uid
	}
	err := r.lifecycle.setDesiredState(mode, sealUID)
	r.mu.Unlock()
	if err != nil {
		return err
	}
	for _, s := range r.sessions.inventory() {
		if err := r.sessions.reopen(ctx, s); err != nil {
			return err
		}
		if err := r.sessions.resumeRecovered(s); err != nil {
			return err
		}
	}
	r.changed()
	return nil
}

func (r *Runner) ensureWorkspace(ctx context.Context, cwd string) (string, error) {
	config := workspace.Config{Root: r.config.WorkspaceRoot}
	if r.workspaceSupervisor != nil {
		config.LaunchHelper = func(ctx context.Context, processConfig supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
			return r.launchProcess(ctx, "workspace-helper", r.workspaceSupervisor, processConfig)
		}
	}
	return workspace.Ensure(ctx, cwd, config)
}

func (r *Runner) Quiesce(_ context.Context, q *runnerpb.QuiesceRequest) (*runnerpb.QuiesceResponse, error) {
	_, err := r.facts()
	if err != nil {
		return nil, rpcError(err)
	}
	r.mu.Lock()
	desiredState, progress := r.lifecycle.desiredState(), r.lifecycle.status()
	if desiredState.Mode == runnerlifecycle.Closing && progress.Generation == desiredState.Generation && progress.Stage == runnerlifecycle.Closed {
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
		mode := runnerlifecycle.Quiescing
		if r.lifecycle.desiredState().Mode == runnerlifecycle.Closing {
			mode = runnerlifecycle.Closing
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
		err := r.lifecycle.setDesiredState(runnerlifecycle.Closing, "")
		r.mu.Unlock()
		r.changed()
		if err == nil {
			err = r.lifecycle.wait(r.ctx, runnerlifecycle.Closed)
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
		for _, s := range r.sessions.inventory() {
			s.work.Wait()
			errs = append(errs, s.journal.Close())
		}
		if r.bootstrapHost != nil {
			errs = append(errs, r.bootstrapHost.CloseLog())
		}
		if err := errors.Join(errs...); err != nil {
			r.closeErr = err
			return
		}
		r.closeErr = r.owner.Close()
	})
	return r.closeErr
}
