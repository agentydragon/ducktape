package harnesshost

import (
	"context"
	"errors"
	"path/filepath"
	"sync"
	"sync/atomic"

	"github.com/agentydragon/ducktape/agentplane/env/harnesshost/codexappserver"
	"github.com/agentydragon/ducktape/agentplane/env/harnesshost/retained"
	"github.com/agentydragon/ducktape/agentplane/env/harnesshost/session"
	"github.com/agentydragon/ducktape/agentplane/env/harnesshost/workspace"
	runnerlifecycle "github.com/agentydragon/ducktape/agentplane/env/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/agentydragon/ducktape/agentplane/env/scripts"
	"github.com/agentydragon/ducktape/agentplane/env/supervision"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/google/uuid"
	"go.uber.org/zap"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type Runner struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
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
	sessions            *session.Manager
	createMu            sync.Mutex
	codexAppServer      *codexappserver.Host
	changes             chan struct{}
	fatal               chan error
	idleSince           *timestamppb.Timestamp
	lifecycle           *lifecycle
	closeOnce           sync.Once
	closeDone           chan struct{}
	closeErr            error
	closing             bool
	intent              *runtimepb.LifecycleIntent
	bootstrapBinding    *runtimepb.Binding
	ready               atomic.Bool
	nativeStopped       bool
	runtimeClient       runtimepb.EnvironmentRuntimeClient
}

func New(config Config) (*Runner, error) {
	if config.RuntimeSocket == "" || config.ListenSocket == "" {
		return nil, errors.New("runtime and harness-host socket paths are required")
	}
	return newRunner(config, nil)
}

func newRunner(config Config, lifecycleState state.State) (*Runner, error) {
	if err := config.resolveSessionReconcileConcurrency(); err != nil {
		return nil, err
	}
	if _, present := config.HarnessEnvironment["CODEX_EXEC_SERVER_URL"]; present {
		return nil, errors.New("CODEX_EXEC_SERVER_URL is owned by execution configuration")
	}
	// The runtime retains the legacy environment lock. This private lock fences only
	// native state ownership; retained Session paths and journal identities stay put.
	owner, err := retained.OpenOwner(filepath.Join(config.StateDir, "harness-host"))
	if err != nil {
		return nil, err
	}
	supervisor := supervision.NewSubprocessSupervisor(owner.File())
	var workspaceSupervisor *supervision.Supervisor
	if config.VMProcessLauncher != nil {
		supervisor, err = config.VMProcessLauncher.PrepareForOwner(config.StateDir, config.Launch.Sandbox.UID, supervision.SystemdOwner{Unit: "agentplane-harness-host.service", LifetimeDirectory: "/run/agentplane-harness-host-lifetime", ScopeID: "harness-host"})
		if err != nil {
			_ = owner.Close()
			return nil, err
		}
		workspaceSupervisor = supervisor
	}
	store, err := retained.NewStore(filepath.Join(config.StateDir, "sessions"))
	if err != nil {
		_ = owner.Close()
		return nil, err
	}
	ctx, cancel := context.WithCancel(context.Background())
	r := &Runner{config: config, owner: owner, supervisor: supervisor, workspaceSupervisor: workspaceSupervisor, store: store, bootID: uuid.NewString(), ctx: ctx, cancel: cancel, changes: make(chan struct{}), fatal: make(chan error, 1)}
	r.scripts, err = scripts.New(scripts.Config{HarnessEnvironment: config.HarnessEnvironment, LaunchProcess: func(ctx context.Context, owner string, cfg supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
		return r.launchProcess(ctx, owner, r.supervisor, cfg)
	}})
	if err != nil {
		cancel()
		_ = owner.Close()
		return nil, err
	}
	l, err := newLifecycle(r, lifecycleState)
	if err != nil {
		cancel()
		_ = owner.Close()
		return nil, err
	}
	r.lifecycle = l
	if err := r.configureSessions(); err != nil {
		cancel()
		_ = owner.Close()
		return nil, err
	}
	go l.run()
	return r, nil
}

func (r *Runner) configureSessions() error {
	var err error
	r.sessions, err = session.NewManager(session.Config{
		Context:            r.ctx,
		State:              r.lifecycle.state,
		Store:              r.store,
		WorkspaceRoot:      r.config.WorkspaceRoot,
		Destination:        r.config.Launch.Destination,
		Claude:             r.config.Claude,
		HarnessEnvironment: r.config.HarnessEnvironment,
		DebugCheckpoint:    r.config.DebugCheckpoint,
		Scripts:            r.scripts,
		CodexAppServer:     r.codexAppServer,
		WaitBootstrap:      r.waitBootstrap,
		EnsureWorkspace:    r.ensureWorkspace,
		LaunchProcess: func(ctx context.Context, owner string, cfg supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
			return r.launchProcess(ctx, owner, r.supervisor, cfg)
		},
		ResolveModelConfig:   r.config.ResolveModelConfig,
		PublishSeal:          r.lifecycle.publishSeal,
		PublishAck:           r.lifecycle.publishAck,
		OnChange:             r.changed,
		OnError:              r.fail,
		ReconcileConcurrency: r.config.SessionReconcileConcurrency,
		Environment:          r.environment,
	})
	if err != nil {
		return err
	}
	return r.lifecycle.engine.RegisterQController(r.sessions.Controller())
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
	zap.L().Error("runner operation failed", zap.Error(err))
	select {
	case r.fatal <- err:
	default:
	}
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

// Close starts local process cleanup once. Canceling ctx ends only this caller's wait;
// accepted setup/bootstrap and the shared cleanup continue under Runner ownership.
func (r *Runner) Close(ctx context.Context) error {
	r.closeOnce.Do(func() {
		r.mu.Lock()
		r.closing = true
		r.mu.Unlock()
		r.closeDone = make(chan struct{})
		go func() {
			r.closeErr = r.finishClose()
			close(r.closeDone)
		}()
	})
	select {
	case <-r.closeDone:
		return r.closeErr
	default:
	}
	select {
	case <-r.closeDone:
		return r.closeErr
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (r *Runner) isClosing() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.closing
}

func (r *Runner) finishClose() error {
	r.mu.Lock()
	err := r.lifecycle.setDesiredState(runnerlifecycle.Closing, "")
	r.mu.Unlock()
	r.changed()
	if err == nil {
		err = r.lifecycle.wait(r.ctx, runnerlifecycle.Closed)
	}
	if err != nil {
		return err
	}
	// Controllers stop before joining workers, so no new operation can race Wait.
	r.cancel()
	<-r.lifecycle.done.Done()
	r.lifecycle.workers.Wait()
	errs := []error{r.sessions.Close()}

	if err := errors.Join(errs...); err != nil {
		return err
	}
	return r.owner.Close()
}
