package runtime

import (
	"context"
	"errors"
	"path/filepath"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/codexappserver"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/retained"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/session"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/workspace"
	"github.com/agentydragon/ducktape/agentplane/env/scripts"
)

func (r *Runtime) initializeSessions() error {
	if err := r.config.resolveSessionReconcileConcurrency(); err != nil {
		return err
	}
	if _, present := r.config.HarnessEnvironment["CODEX_EXEC_SERVER_URL"]; present {
		return errors.New("CODEX_EXEC_SERVER_URL is owned by execution configuration")
	}
	var err error
	r.store, err = retained.NewStore(filepath.Join(r.config.StateDir, "sessions"), r.lifecycleState)
	if err != nil {
		return err
	}
	r.harnessProcesses = r.processes.Scope(r.lifecycleContext, supervision.HarnessDomain, &r.sessionWorkers, r.fail)
	if r.config.Codex != nil {
		execURL := ""
		if r.config.CodexExecServer != nil {
			execURL = r.config.CodexExecServer.URL
		}
		r.codexAppServer, err = codexappserver.NewHost(*r.config.Codex, codexappserver.Dependencies{
			Context: r.lifecycleContext, State: r.lifecycleState, Workers: &r.sessionWorkers,
			Processes: r.harnessProcesses, Supervisor: r.harnessSupervisor,
			HarnessEnvironment: r.config.HarnessEnvironment, StateDir: r.config.StateDir,
			RequestAdmission: r.admitCodexRequest, Admission: r.admitLifecycleEffect,
			ExecServerURL: execURL, ExecutionReady: r.executionReady,
			Readiness: r.config.CodexAppServerReadiness, OnError: r.fail, OnChange: r.changed,
		})
		if err != nil {
			return err
		}
		if err := r.codexAppServer.Initialize(r.lifecycleContext); err != nil {
			return err
		}
	}
	r.sessionScripts, err = scripts.New(scripts.Config{
		HarnessEnvironment: r.config.HarnessEnvironment,
		LaunchProcess: func(ctx context.Context, owner string, config supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
			return r.launchSessionProcess(ctx, owner, r.harnessSupervisor, config)
		},
	})
	if err != nil {
		return err
	}
	sessions, err := session.NewManager(session.Config{
		Context: r.lifecycleContext, State: r.lifecycleState, Store: r.store,
		WorkspaceRoot: r.config.WorkspaceRoot,
		Claude:        r.config.Claude, HarnessEnvironment: r.config.HarnessEnvironment,
		DebugCheckpoint: r.config.DebugCheckpoint, Scripts: r.sessionScripts,
		CodexAppServer: r.codexAppServer, WaitBootstrap: r.waitBootstrapForSession,
		EnsureWorkspace: r.ensureWorkspace,
		LaunchProcess: func(ctx context.Context, owner string, config supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
			return r.launchSessionProcess(ctx, owner, r.harnessSupervisor, config)
		},
		ResolveModelConfig: r.config.ResolveModelConfig,
		OnChange:           r.changed, OnError: r.fail,
		ReconcileConcurrency: r.config.SessionReconcileConcurrency, Environment: r.environment,
		Admission: r.admitLifecycleEffect,
	})
	if err != nil {
		return err
	}
	r.mu.Lock()
	r.sessions = sessions
	r.mu.Unlock()
	if err := r.initializeSessionLifecycle(); err != nil {
		return err
	}
	if r.codexAppServer != nil {
		if err := r.lifecycleEngine.RegisterQController(r.codexAppServer.Controller()); err != nil {
			return err
		}
	}
	if err := r.lifecycleEngine.RegisterQController(r.sessions.OwnerController()); err != nil {
		return err
	}
	return r.lifecycleEngine.RegisterQController(r.sessions.Controller())
}

func (r *Runtime) ensureWorkspace(ctx context.Context, cwd string) (string, error) {
	config := workspace.Config{Root: r.config.WorkspaceRoot}
	if r.config.VMProcessLauncher != nil {
		config.LaunchHelper = func(ctx context.Context, processConfig supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
			return r.launchSessionProcess(ctx, "workspace-helper", r.harnessSupervisor, processConfig)
		}
	}
	return workspace.Ensure(ctx, cwd, config)
}
