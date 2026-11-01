// Package runtime owns one Sandbox environment's identity, bootstrap and component lifecycles.
package runtime

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"
	"sort"
	"sync"

	"github.com/agentydragon/ducktape/agentplane/env/configuration"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/bootstrap"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/control"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	executorpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/executorpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/harnesshost"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/lifecycle"
	runtimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/runtimepb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/agentydragon/ducktape/agentplane/env/scripts"
	"github.com/agentydragon/ducktape/agentplane/observability"
	environmentruntimestatepb "github.com/agentydragon/ducktape/agentplane/proto/environmentruntimestatepb"
	sandboxpb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	cosiruntime "github.com/cosi-project/runtime/pkg/controller/runtime"
	"github.com/cosi-project/runtime/pkg/state"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	"github.com/google/uuid"
	"go.uber.org/zap"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

const (
	RuntimeUnitName                            = "agentplane-environment-runtime.service"
	RuntimeScopeID  supervision.SystemdScopeID = supervision.SystemdEnvironmentRuntimeScopeID
	HostScopeID     supervision.SystemdScopeID = "harness-host"
)

const (
	runtimeBootMetadata = "agentplane-runtime-boot-id"
	hostBootMetadata    = "agentplane-harness-host-boot-id"
	generationMetadata  = "agentplane-environment-generation"
)

// Config contains the resolved environment and embedded Host configuration.
type Config struct {
	Control            configuration.EnvironmentRuntimeControlConfiguration
	StateDir           string
	Launch             configuration.SandboxLaunch
	Supervision        string
	PodPID1            bool
	VMProcessLauncher  *supervision.SystemdLauncher
	CodexExecServer    *codexexecserver.Config
	CodexBinary        string
	HarnessEnvironment map[string]string
	InitializationCwd  string
	HostConfig         harnesshost.Config
	HostIngress        configuration.Endpoint
	RuntimeSocket      string
	HostSocket         string
	ExecutionSocket    string
}

type hostLifetime interface {
	Stop(context.Context) error
	Wait(context.Context) (int, error)
}

type childExit struct {
	code int
	err  error
}

type runtimeDeps struct {
	prepareSupervisor func(Config, *os.File) (*supervision.Supervisor, error)
	dialHost          func(string) (*grpc.ClientConn, error)
	listen            func(string) (net.Listener, error)
	waitBootstrap     func(context.Context) error
	startExecution    func(context.Context, Config, string) (executionHandle, error)
}

type executionHandle interface {
	ClientConn() *grpc.ClientConn
	StopAndFence(context.Context) error
	Close() error
}

type hostLink struct {
	bootID        string
	controlSocket string
	conn          *grpc.ClientConn
	client        environmentruntimepb.EnvironmentRuntimeClient
	intent        *runtimepb.LifecycleIntent
	report        *runtimepb.HostReport
	intentChanged chan struct{}
	connected     bool
}

// Runtime is both the private EnvironmentRuntime lifecycle server and the Gateway-facing Runner
// backend. Gateway epoch admission remains in env/runtime/control; every forwarded Host call carries the
// exact Runtime/Host/generation binding.
type Runtime struct {
	environmentruntimepb.UnimplementedEnvironmentRuntimeServer
	config Config
	deps   runtimeDeps

	mu                   sync.Mutex
	transitionMu         sync.Mutex
	executionMu          sync.Mutex
	owner                *environmentOwner
	state                *EnvironmentState
	lifecycleState       state.State
	lifecycleEngine      *cosiruntime.Runtime
	lifecycleCancel      context.CancelFunc
	lifecycleDone        chan struct{}
	lifecycleWorkers     sync.WaitGroup
	processes            *supervision.Host
	scripts              *scripts.Runner
	bootstrapHost        *bootstrap.Host
	desired              lifecycle.Desired
	bootID               string
	supervisor           *supervision.Supervisor
	host                 *hostLink
	intent               *runtimepb.LifecycleIntent
	lastRunning          *runtimepb.Binding
	executorProxy        executorpb.ExecutorServer
	executionConn        *grpc.ClientConn
	executionOwner       executionHandle
	executionUID         string
	executionRecovering  bool
	activeAdmissions     uint64
	activeExecutionCalls uint64
	activeExecStreams    uint64
	execStreamID         uint64
	execStreams          map[uint64]context.CancelFunc
	execCalls            map[uint64]context.CancelFunc
	execCallID           uint64
	executionFence       bool
	fenceInFlight        *runtimepb.Binding
	closing              bool
	failed               bool
	idleSince            *timestamppb.Timestamp
	changes              chan struct{}
	fatal                chan error
	failureErr           error
	control              *control.Server
	waitBootstrap        func(context.Context) error
	hostLifetime         hostLifetime
	hostExited           bool
	hostExit             chan childExit
	closeOnce            sync.Once
	closeDone            chan struct{}
	closeErr             error
}

func New(config Config) (*Runtime, error) {
	return newRuntime(config, nil, runtimeDeps{})
}

// newRuntime is the deterministic test seam used by lifecycle/forwarding tests. A nil COSI state
// selects an in-memory Runner lifecycle store; a nonnil one lets focused tests inject controller
// state without changing the production process topology.
func newRuntime(config Config, injectedState state.State, deps runtimeDeps) (*Runtime, error) {
	if !filepath.IsAbs(config.StateDir) {
		return nil, errors.New("environment state directory must be absolute")
	}
	if config.Launch.Sandbox.Namespace == "" || config.Launch.Sandbox.Name == "" || config.Launch.Sandbox.UID == "" {
		return nil, errors.New("environment Runtime requires Sandbox namespace, name and UID")
	}
	if err := configuration.ValidateControlIdentity(config.Control, config.Launch.Sandbox); err != nil {
		return nil, err
	}
	if config.Supervision != "pid1" && config.Supervision != "subprocess" && config.Supervision != "systemd" {
		return nil, errors.New("environment Runtime requires explicit pid1, subprocess or systemd supervision")
	}
	if config.Supervision == "pid1" && (!config.PodPID1 || config.VMProcessLauncher != nil) {
		return nil, errors.New("PID1 Runtime supervision requires Pod PID1 configuration")
	}
	if config.Supervision == "subprocess" && (config.PodPID1 || config.VMProcessLauncher != nil) {
		return nil, errors.New("subprocess Runtime supervision requires Pod subprocess configuration")
	}
	if config.Supervision == "systemd" && (config.PodPID1 || config.VMProcessLauncher == nil) {
		return nil, errors.New("systemd Runtime supervision requires VM launcher configuration")
	}
	if config.Supervision == "pid1" && os.Getpid() != 1 && deps.prepareSupervisor == nil {
		return nil, errors.New("Pod environment Runtime must be PID 1")
	}
	if config.CodexExecServer != nil && (config.CodexBinary == "" || config.HarnessEnvironment == nil) {
		return nil, errors.New("execution service requires Codex binary and resolved harness environment")
	}
	if config.RuntimeSocket == "" {
		if config.Supervision == "systemd" {
			config.RuntimeSocket = filepath.Join(config.StateDir, "runtime", "lifecycle.sock")
		} else {
			config.RuntimeSocket = filepath.Join(config.StateDir, "r.sock")
		}
	}
	if config.HostSocket == "" {
		if config.Supervision == "systemd" {
			config.HostSocket = filepath.Join(config.StateDir, "harness-host", "control.sock")
		} else {
			config.HostSocket = filepath.Join(config.StateDir, "h.sock")
		}
	}
	if config.ExecutionSocket == "" {
		if config.Supervision == "systemd" {
			config.ExecutionSocket = filepath.Join(config.StateDir, "execution", "execution.sock")
		} else {
			config.ExecutionSocket = filepath.Join(config.StateDir, "e.sock")
		}
	}
	for name, path := range map[string]string{"Runtime socket": config.RuntimeSocket, "Host socket": config.HostSocket, "Execution socket": config.ExecutionSocket} {
		if !filepath.IsAbs(path) {
			return nil, fmt.Errorf("%s path must be absolute", name)
		}
	}
	owner, err := openEnvironmentOwner(config.StateDir)
	if err != nil {
		return nil, err
	}
	environmentState, err := NewEnvironmentState(config.StateDir)
	if err != nil {
		_ = owner.Close()
		return nil, err
	}
	supervisor := (*supervision.Supervisor)(nil)
	if deps.prepareSupervisor != nil {
		supervisor, err = deps.prepareSupervisor(config, owner.file)
	} else if config.Supervision == "pid1" {
		supervisor, err = supervision.NewPID1Supervisor(owner.file)
	} else if config.Supervision == "subprocess" {
		supervisor = supervision.NewSubprocessSupervisor(owner.file)
	} else {
		supervisor, err = config.VMProcessLauncher.PrepareForOwner(config.StateDir, config.Launch.Sandbox.UID, supervision.SystemdOwner{
			Unit: RuntimeUnitName, LifetimeDirectory: "/run/agentplane-environment-runtime-lifetime", ScopeID: RuntimeScopeID,
		})
	}
	if err != nil {
		_ = owner.Close()
		return nil, err
	}
	if deps.dialHost == nil {
		deps.dialHost = dialUnix
	}
	if deps.listen == nil {
		deps.listen = func(address string) (net.Listener, error) { return net.Listen("unix", address) }
	}
	lifecycleState := injectedState
	if lifecycleState == nil {
		lifecycleState = state.WrapCore(namespaced.NewState(inmem.Build))
	}
	runtimeCtx, cancelRuntime := context.WithCancel(context.Background())
	r := &Runtime{
		config: config, deps: deps, owner: owner, state: environmentState, supervisor: supervisor,
		lifecycleState: lifecycleState, lifecycleCancel: cancelRuntime, lifecycleDone: make(chan struct{}),
		bootID: uuid.NewString(), changes: make(chan struct{}), fatal: make(chan error, 1), waitBootstrap: deps.waitBootstrap,
		desired: lifecycle.Desired{Mode: lifecycle.Quiescing, Generation: 1}, closeDone: make(chan struct{}),
		execStreams: make(map[uint64]context.CancelFunc), execCalls: make(map[uint64]context.CancelFunc),
	}
	if err := r.initializeLifecycle(runtimeCtx); err != nil {
		cancelRuntime()
		_ = owner.Close()
		return nil, err
	}
	if r.deps.startExecution == nil {
		r.deps.startExecution = r.startExecutionService
	}
	r.control = control.New(control.Config{
		SandboxUID: config.Launch.Sandbox.UID, BootID: r.bootID,
		ListenAddress: config.Control.ListenAddress, Security: config.Control.Security,
		IsClosing: r.isClosing, AdoptIncarnation: r.adoptIncarnation,
		Facts: r.facts, Changes: r.changesSince,
	}, r)
	r.executorProxy = newExecutionProxy(r)
	return r, nil
}

func (r *Runtime) initializeLifecycle(ctx context.Context) error {
	engine, err := cosiruntime.NewRuntime(r.lifecycleState, zap.L().Named("environment-runtime-lifecycle"))
	if err != nil {
		return err
	}
	r.lifecycleEngine = engine
	r.processes = supervision.NewHost(ctx, r.lifecycleState, lifecycle.Namespace, &r.lifecycleWorkers, r.fail)
	r.scripts, err = scripts.New(scripts.Config{
		HarnessEnvironment: r.config.HarnessEnvironment,
		LaunchProcess: func(processCtx context.Context, owner string, processConfig supervision.ProcessConfig) (*supervision.ManagedProcess, error) {
			r.mu.Lock()
			generation := r.desired.Generation
			r.mu.Unlock()
			return r.processes.Launch(processCtx, owner, generation, r.supervisor, processConfig)
		},
	})
	if err != nil {
		return err
	}
	r.bootstrapHost, err = bootstrap.NewHost(bootstrap.Config{
		Script: r.config.Launch.Bootstrap, StateDir: r.config.StateDir,
		WorkingDirectory: r.config.InitializationCwd,
	}, bootstrap.Dependencies{
		Context: ctx, State: r.lifecycleState, Workers: &r.lifecycleWorkers, Scripts: r.scripts,
		Admission: r.admitLifecycleEffect, OnError: r.fail, OnRuntimeChange: r.changed,
	})
	if err != nil {
		return err
	}
	if err := r.bootstrapHost.Initialize(ctx); err != nil {
		return err
	}
	if err := lifecycle.PublishDesired(ctx, r.lifecycleState, r.desired); err != nil {
		return err
	}
	if err := engine.RegisterQController(r.processes.Controller()); err != nil {
		return err
	}
	if err := engine.RegisterQController(r.bootstrapHost.Controller()); err != nil {
		return err
	}
	go func() {
		defer close(r.lifecycleDone)
		if err := engine.Run(ctx); err != nil && !errors.Is(err, context.Canceled) {
			r.fail(fmt.Errorf("environment Runtime lifecycle engine stopped: %w", err))
		}
	}()
	return nil
}

// admitLifecycleEffect serializes bootstrap admission with Runtime generation changes.
func (r *Runtime) admitLifecycleEffect(ctx context.Context, expected lifecycle.Desired, effect func(context.Context) error) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if err := ctx.Err(); err != nil {
		return err
	}
	if r.closing || r.desired != expected || r.desired.Mode != lifecycle.Running {
		return nil
	}
	return effect(ctx)
}

// Close stops Runtime-owned bootstrap work and releases the legacy environment-owner lock.
// Canceling ctx only stops this caller's wait; shutdown and worker joining continue.
func (r *Runtime) Close(ctx context.Context) error {
	r.mu.Lock()
	r.closing = true
	r.changedLocked()
	r.mu.Unlock()
	r.closeOnce.Do(func() {
		go func() {
			r.closeErr = r.finishClose()
			close(r.closeDone)
		}()
	})
	select {
	case <-r.closeDone:
		return r.closeErr
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (r *Runtime) finishClose() error {
	r.mu.Lock()
	r.desired = lifecycle.Desired{Mode: lifecycle.Closing, Generation: r.desired.Generation + 1}
	publishErr := lifecycle.PublishDesired(context.Background(), r.lifecycleState, r.desired)
	snapshot := r.state.Snapshot()
	sealUID := ""
	if snapshot.Quiesced {
		sealUID = snapshot.IncarnationUID
	}
	if sealedUID, sealed, err := r.state.SealedUID(); err != nil {
		r.mu.Unlock()
		return fmt.Errorf("read environment seal during shutdown: %w", err)
	} else if sealed {
		sealUID = sealedUID
	}
	incarnationUID := snapshot.IncarnationUID
	if r.intent != nil && incarnationUID == "" {
		incarnationUID = r.intent.IncarnationUid
	}
	if sealUID != "" {
		incarnationUID = sealUID
	}
	connected := r.host != nil && r.host.connected && !r.hostExited
	executionExists := r.executionOwner != nil
	hostLifetimeExists := r.hostLifetime != nil
	var binding *runtimepb.Binding
	var intentMode runtimepb.LifecycleIntent_Mode
	if connected {
		intentMode = runtimepb.LifecycleIntent_CLOSING
		if sealUID != "" {
			intentMode = runtimepb.LifecycleIntent_QUIESCING
		}
		if sealUID != "" && r.intent != nil && r.intent.Mode == runtimepb.LifecycleIntent_QUIESCING && r.intent.SealUid == sealUID {
			binding = proto.Clone(r.intent.Binding).(*runtimepb.Binding)
		} else {
			intent := r.setIntentLocked(intentMode, incarnationUID, sealUID, false)
			binding = proto.Clone(intent.Binding).(*runtimepb.Binding)
		}
	}
	r.mu.Unlock()

	if publishErr != nil {
		return fmt.Errorf("publish Runtime closing state: %w", publishErr)
	}
	fenceCtx, cancelFence := context.WithTimeout(context.Background(), executionFenceGrace)
	defer cancelFence()
	var hostRunErr error
	if connected {
		if err := r.waitForHostNativeStopped(fenceCtx, binding); err != nil {
			return fmt.Errorf("Harness Host did not prove native work stopped: %w", err)
		}
		r.maybeFenceExecution(binding)
		if err := r.waitForExecutionFenced(fenceCtx, binding); err != nil {
			return fmt.Errorf("embedded Execution API did not prove its backend fence: %w", err)
		}
		if sealUID != "" {
			if err := r.waitForHostQuiesced(fenceCtx, binding); err != nil {
				return fmt.Errorf("Harness Host did not flush the sealed generation: %w", err)
			}
		}
	} else if hostLifetimeExists {
		// A lost private channel is still a service failure, but the embedded
		// Host can prove cleanup directly. Join it before fencing Execution.
		if err := r.hostLifetime.Stop(fenceCtx); err != nil {
			return fmt.Errorf("Runtime lost its Host lifecycle connection and native cleanup is unproved: %w", err)
		}
		_, hostRunErr = r.hostLifetime.Wait(fenceCtx)
		r.cancelExecutionCalls()
		if err := r.waitForExecutionCalls(fenceCtx); err != nil {
			return fmt.Errorf("drain Execution calls after Host lifecycle loss: %w", err)
		}
		if err := r.stopExecution(fenceCtx); err != nil {
			return fmt.Errorf("fence Execution after Host lifecycle loss: %w", err)
		}
		hostRunErr = errors.Join(errors.New("Harness Host lifecycle connection was lost"), hostRunErr)
	} else if executionExists {
		return errors.New("Runtime lost its Host lifecycle connection before proving native cleanup")
	}

	if connected {
		if err := r.stopHost(fenceCtx); err != nil {
			return fmt.Errorf("stop and fence Harness Host: %w", err)
		}
	}
	r.mu.Lock()
	if r.host != nil && r.host.conn != nil {
		_ = r.host.conn.Close()
		r.host.connected = false
	}
	r.mu.Unlock()
	if err := r.waitBootstrapSettled(); err != nil {
		return err
	}
	r.lifecycleCancel()
	<-r.lifecycleDone
	r.lifecycleWorkers.Wait()
	if r.config.Supervision == "pid1" {
		// The Runtime owns the whole Pod PID namespace. Join both management
		// controllers before killing any escaped native descendants, then retain
		// the state-owner lock until the namespace fence has completed.
		if err := r.supervisor.FenceNamespace(context.Background()); err != nil {
			return fmt.Errorf("fence Pod PID namespace: %w", err)
		}
	}
	var errs []error
	if r.bootstrapHost != nil {
		errs = append(errs, r.bootstrapHost.CloseLog())
	}
	if err := errors.Join(errs...); err != nil {
		return err
	}
	return errors.Join(hostRunErr, r.owner.Close())
}

func (r *Runtime) waitBootstrapSettled() error {
	if r.bootstrapHost == nil || !r.bootstrapHost.Configured() {
		return nil
	}
	input, found, err := bootstrap.ReadInput(context.Background(), r.lifecycleState)
	if err != nil {
		return fmt.Errorf("read accepted bootstrap state during shutdown: %w", err)
	}
	if !found || !input.Requested {
		return nil
	}
	// A terminal bootstrap error is its durable outcome, not a failure to fence the
	// Runtime-owned process. Settled checks that the accepted operation and cleanup ended.
	_ = r.bootstrapHost.Wait(context.Background())
	settled, err := r.bootstrapHost.Settled(context.Background())
	if err != nil {
		return fmt.Errorf("observe accepted bootstrap cleanup: %w", err)
	}
	if !settled {
		return errors.New("accepted bootstrap did not settle before Runtime shutdown")
	}
	return nil
}

func (r *Runtime) stopHost(ctx context.Context) error {
	r.mu.Lock()
	process := r.hostLifetime
	r.mu.Unlock()
	if process == nil {
		return nil
	}
	stopErr := process.Stop(ctx)
	code, waitErr := process.Wait(ctx)
	if code != 0 {
		waitErr = errors.Join(waitErr, fmt.Errorf("Harness Host exited with status %d", code))
	}
	return errors.Join(stopErr, waitErr)
}

func (r *Runtime) changed() {
	r.mu.Lock()
	r.changedLocked()
	r.mu.Unlock()
}

func (r *Runtime) waitForHostNativeStopped(ctx context.Context, binding *runtimepb.Binding) error {
	for {
		r.mu.Lock()
		if failure := r.failureLocked(); failure != nil {
			r.mu.Unlock()
			return failure
		}
		host := r.host
		if host == nil || !host.connected {
			r.mu.Unlock()
			return errors.New("Harness Host lifecycle connection was lost before native-stop acknowledgement")
		}
		if host.report != nil && host.report.Ready && host.report.NativeStopped && proto.Equal(host.report.Binding, binding) {
			r.mu.Unlock()
			return nil
		}
		changed := r.changes
		r.mu.Unlock()
		select {
		case <-changed:
		case <-ctx.Done():
			return ctx.Err()
		}
	}
}

func (r *Runtime) adoptIncarnation(ctx context.Context, uid string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if uid == "" {
		return errors.New("Runner incarnation UID is required")
	}
	r.transitionMu.Lock()
	defer r.transitionMu.Unlock()
	r.mu.Lock()
	if r.closing {
		r.mu.Unlock()
		return status.Error(codes.Unavailable, "environment Runtime is closing")
	}
	if r.host == nil || !r.host.connected {
		r.mu.Unlock()
		return status.Error(codes.Unavailable, "Harness Host lifecycle connection is unavailable")
	}
	oldUID, known := r.state.IncarnationUID()
	initialInactive := r.intent != nil && r.intent.Mode == runtimepb.LifecycleIntent_INACTIVE
	var initialBinding *runtimepb.Binding
	if initialInactive {
		initialBinding = proto.Clone(r.intent.Binding).(*runtimepb.Binding)
	}
	r.mu.Unlock()
	if initialInactive {
		if err := r.waitForHostNativeStopped(ctx, initialBinding); err != nil {
			return status.Errorf(codes.Unavailable, "initial Harness Host generation did not establish its native-stop barrier: %v", err)
		}
	}

	sealedUID, sealed, err := r.state.SealedUID()
	if err != nil {
		return status.Errorf(codes.Unavailable, "read environment seal: %v", err)
	}
	if sealed {
		oldUID = sealedUID
		known = true
	}
	if known && oldUID == uid && !sealed {
		r.mu.Lock()
		alreadyRunning := r.intent != nil && r.intent.Mode == runtimepb.LifecycleIntent_RUNNING &&
			r.intent.IncarnationUid == uid && r.host != nil && r.host.report != nil && r.host.report.Ready &&
			proto.Equal(r.host.report.Binding, r.intent.Binding)
		binding := (*runtimepb.Binding)(nil)
		mode := runtimepb.LifecycleIntent_INACTIVE
		if r.intent != nil {
			binding = proto.Clone(r.intent.Binding).(*runtimepb.Binding)
			mode = r.intent.Mode
		}
		r.mu.Unlock()
		if alreadyRunning {
			r.setLastRunning(binding)
			return r.requestBootstrap(ctx)
		}
		if mode == runtimepb.LifecycleIntent_RUNNING {
			if err := r.startExecutionForUID(ctx, uid); err != nil {
				return err
			}
			if _, err := r.waitForHostReady(ctx, binding); err != nil {
				return status.Errorf(codes.Unavailable, "Harness Host did not apply the active generation: %v", err)
			}
			r.setLastRunning(binding)
			return r.requestBootstrap(ctx)
		}
		if mode == runtimepb.LifecycleIntent_QUIESCING || mode == runtimepb.LifecycleIntent_CLOSING {
			if err := r.waitForHostNativeStopped(ctx, binding); err != nil {
				return status.Errorf(codes.Unavailable, "prior Harness Host generation did not drain: %v", err)
			}
			r.maybeFenceExecution(binding)
			if err := r.waitForExecutionFenced(ctx, binding); err != nil {
				return status.Errorf(codes.Unavailable, "prior Execution generation did not fence: %v", err)
			}
		} else if mode == runtimepb.LifecycleIntent_INACTIVE {
			if err := r.waitForHostNativeStopped(ctx, binding); err != nil {
				return status.Errorf(codes.Unavailable, "initial Harness Host generation did not drain: %v", err)
			}
		} else {
			return status.Errorf(codes.Unavailable, "cannot resume environment from lifecycle mode %s", mode)
		}
		if err := r.startExecutionForUID(ctx, uid); err != nil {
			return err
		}
		binding, err = r.publishIntent(ctx, lifecycle.Running, runtimepb.LifecycleIntent_RUNNING, uid, "")
		if err != nil {
			return err
		}
		if _, err := r.waitForHostReady(ctx, binding); err != nil {
			return status.Errorf(codes.Unavailable, "Harness Host did not apply the active generation: %v", err)
		}
		r.setLastRunning(binding)
		return r.requestBootstrap(ctx)
	}

	if known {
		if err := r.startExecutionForUID(ctx, oldUID); err != nil {
			return fmt.Errorf("prepare old incarnation %s for drain: %w", oldUID, err)
		}
		mode := runtimepb.LifecycleIntent_CLOSING
		seal := ""
		if sealed {
			mode, seal = runtimepb.LifecycleIntent_QUIESCING, oldUID
		}
		binding, err := r.publishIntent(ctx, lifecycle.Quiescing, mode, oldUID, seal)
		if err != nil {
			return err
		}
		if err := r.waitForHostNativeStopped(ctx, binding); err != nil {
			return status.Errorf(codes.Unavailable, "old Harness Host generation did not drain: %v", err)
		}
		if err := r.waitForExecutionFenced(ctx, binding); err != nil {
			return status.Errorf(codes.Unavailable, "old Execution generation did not fence: %v", err)
		}
		if sealed {
			if err := r.waitForHostQuiesced(ctx, binding); err != nil {
				return status.Errorf(codes.Unavailable, "old sealed Harness Host generation did not flush: %v", err)
			}
			if oldUID == uid {
				if err := r.state.AdoptIncarnation(uid); err != nil {
					return status.Errorf(codes.Unavailable, "retain existing sealed incarnation: %v", err)
				}
				return nil
			}
		}
	}

	if err := r.state.AdoptIncarnation(uid); err != nil {
		return status.Errorf(codes.Unavailable, "adopt Sandbox incarnation: %v", err)
	}
	if r.state.Lameduck() {
		// The durable marker names this same incarnation. It remains sealed and must not
		// be cleared merely because the Gateway opened a new watch epoch.
		if err := r.startExecutionForUID(ctx, uid); err != nil {
			return err
		}
		binding, err := r.publishIntent(ctx, lifecycle.Quiescing, runtimepb.LifecycleIntent_QUIESCING, uid, uid)
		if err != nil {
			return err
		}
		if report, err := r.waitForHostReady(ctx, binding); err != nil {
			return status.Errorf(codes.Unavailable, "sealed Harness Host generation did not apply: %v", err)
		} else if report.NativeStopped {
			r.maybeFenceExecution(binding)
		}
		if err := r.waitForHostNativeStopped(ctx, binding); err != nil {
			return status.Errorf(codes.Unavailable, "sealed Harness Host generation did not drain: %v", err)
		}
		if err := r.waitForExecutionFenced(ctx, binding); err != nil {
			return status.Errorf(codes.Unavailable, "sealed Execution generation did not fence: %v", err)
		}
		if err := r.waitForHostQuiesced(ctx, binding); err != nil {
			return status.Errorf(codes.Unavailable, "sealed Harness Host generation did not flush: %v", err)
		}
		return nil
	}
	if err := r.startExecutionForUID(ctx, uid); err != nil {
		return err
	}
	binding, err := r.publishIntent(ctx, lifecycle.Running, runtimepb.LifecycleIntent_RUNNING, uid, "")
	if err != nil {
		return err
	}
	if _, err := r.waitForHostReady(ctx, binding); err != nil {
		return status.Errorf(codes.Unavailable, "Harness Host did not apply the active generation: %v", err)
	}
	r.setLastRunning(binding)
	return r.requestBootstrap(ctx)
}

func (r *Runtime) startExecutionForUID(ctx context.Context, uid string) error {
	r.executionMu.Lock()
	defer r.executionMu.Unlock()
	return r.startExecutionForUIDLocked(ctx, uid)
}

func (r *Runtime) startExecutionForUIDLocked(ctx context.Context, uid string) error {
	if r.config.CodexExecServer == nil {
		return nil
	}
	r.mu.Lock()
	current := r.executionOwner
	currentUID := r.executionUID
	recovering := r.executionRecovering
	r.mu.Unlock()
	if current != nil && currentUID == uid {
		if recovering {
			return status.Error(codes.Unavailable, "Execution API is recovering after a fenced backend loss")
		}
		return nil
	}
	if current != nil {
		if err := r.stopExecutionLocked(ctx); err != nil {
			return fmt.Errorf("fence prior Execution service: %w", err)
		}
	}
	handle, err := r.deps.startExecution(ctx, r.config, uid)
	if err != nil {
		return err
	}
	if handle == nil || handle.ClientConn() == nil {
		if handle != nil {
			_ = handle.Close()
		}
		return errors.New("Execution service did not provide a connected client")
	}
	r.mu.Lock()
	if r.closing {
		r.mu.Unlock()
		return errors.Join(status.Error(codes.Unavailable, "environment Runtime is closing"), handle.StopAndFence(context.Background()), handle.Close())
	}
	r.executionOwner, r.executionConn, r.executionUID = handle, handle.ClientConn(), uid
	r.executionRecovering = false
	r.idleSince = nil
	r.changedLocked()
	r.mu.Unlock()
	if monitor, ok := handle.(interface {
		ProcessExit() <-chan childExit
		stopRequested() bool
	}); ok && monitor.ProcessExit() != nil {
		go r.watchExecutionProcess(handle, uid, monitor)
	}
	return nil
}

func (r *Runtime) publishIntent(ctx context.Context, desiredMode lifecycle.Mode, intentMode runtimepb.LifecycleIntent_Mode, uid, sealUID string) (*runtimepb.Binding, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closing || r.host == nil || !r.host.connected {
		return nil, status.Error(codes.Unavailable, "environment Runtime or Harness Host is closing")
	}
	desired := lifecycle.Desired{Mode: desiredMode, SealUID: sealUID, Generation: r.desired.Generation + 1}
	if err := lifecycle.PublishDesired(ctx, r.lifecycleState, desired); err != nil {
		return nil, status.Errorf(codes.Unavailable, "publish environment lifecycle generation: %v", err)
	}
	r.desired = desired
	intent := r.setIntentLocked(intentMode, uid, sealUID, false)
	if intentMode == runtimepb.LifecycleIntent_RUNNING {
		r.lastRunning = proto.Clone(intent.Binding).(*runtimepb.Binding)
	}
	return proto.Clone(intent.Binding).(*runtimepb.Binding), nil
}

func (r *Runtime) setLastRunning(binding *runtimepb.Binding) {
	r.mu.Lock()
	r.lastRunning = proto.Clone(binding).(*runtimepb.Binding)
	r.mu.Unlock()
}

func (r *Runtime) requestBootstrap(ctx context.Context) error {
	if r.bootstrapHost != nil && r.bootstrapHost.Configured() {
		if err := r.bootstrapHost.Request(ctx); err != nil {
			return fmt.Errorf("admit environment bootstrap: %w", err)
		}
	}
	r.changed()
	return nil
}

func (r *Runtime) stopExecution(ctx context.Context) error {
	r.executionMu.Lock()
	defer r.executionMu.Unlock()
	return r.stopExecutionLocked(ctx)
}

func (r *Runtime) stopExecutionLocked(ctx context.Context) error {
	r.mu.Lock()
	handle := r.executionOwner
	r.mu.Unlock()
	if handle == nil {
		return nil
	}
	if err := handle.StopAndFence(ctx); err != nil {
		return err
	}
	if err := handle.Close(); err != nil {
		return err
	}
	r.mu.Lock()
	if r.executionOwner == handle {
		r.executionOwner, r.executionConn, r.executionUID = nil, nil, ""
		r.executionRecovering = false
		r.changedLocked()
	}
	r.mu.Unlock()
	return nil
}

func (r *Runtime) watchExecutionProcess(handle executionHandle, uid string, monitor interface {
	ProcessExit() <-chan childExit
	stopRequested() bool
},
) {
	exit, ok := <-monitor.ProcessExit()
	if !ok || monitor.stopRequested() {
		return
	}
	r.mu.Lock()
	current := r.executionOwner == handle && r.executionUID == uid
	if !current || r.closing {
		r.mu.Unlock()
		return
	}
	if exit.code != 0 || exit.err != nil {
		r.mu.Unlock()
		r.fail(fmt.Errorf("embedded Execution API exited without a successful backend fence (status=%d): %v", exit.code, exit.err))
		return
	}
	// A fenced backend loss still needs an exact fence
	// when the environment is already draining. Recovery owns that fence and
	// clears the old handle before a same-incarnation RUNNING transition can
	// reuse it. It only starts a replacement if RUNNING is still current after
	// the fence completes.
	r.executionRecovering = true
	r.idleSince = nil
	cancels := make([]context.CancelFunc, 0, len(r.execCalls)+len(r.execStreams))
	for _, cancel := range r.execCalls {
		cancels = append(cancels, cancel)
	}
	for _, cancel := range r.execStreams {
		cancels = append(cancels, cancel)
	}
	r.changedLocked()
	r.mu.Unlock()
	for _, cancel := range cancels {
		cancel()
	}
	go r.recoverFencedExecution(handle, uid)
}

func (r *Runtime) recoverFencedExecution(handle executionHandle, uid string) {
	ctx, cancel := context.WithTimeout(context.Background(), executionFenceGrace)
	defer cancel()
	r.transitionMu.Lock()
	defer r.transitionMu.Unlock()
	r.executionMu.Lock()
	defer r.executionMu.Unlock()

	r.mu.Lock()
	if r.executionOwner != handle || r.executionUID != uid || !r.executionRecovering {
		r.mu.Unlock()
		return
	}
	r.mu.Unlock()

	if err := r.waitForExecutionCalls(ctx); err != nil {
		r.fail(fmt.Errorf("drain Execution calls after fenced backend loss: %w", err))
		return
	}
	if err := handle.StopAndFence(ctx); err != nil {
		r.fail(fmt.Errorf("prove Execution service fence after backend health loss: %w", err))
		return
	}
	if err := handle.Close(); err != nil {
		r.fail(fmt.Errorf("close fenced Execution connection: %w", err))
		return
	}
	r.mu.Lock()
	if r.executionOwner == handle {
		r.executionOwner, r.executionConn, r.executionUID = nil, nil, ""
	}
	closing := r.closing
	stillRunning := r.intent != nil && r.intent.Mode == runtimepb.LifecycleIntent_RUNNING && r.intent.IncarnationUid == uid
	if closing || !stillRunning {
		r.executionRecovering = false
		r.changedLocked()
		r.mu.Unlock()
		return
	}
	r.mu.Unlock()

	if err := r.startExecutionForUIDLocked(ctx, uid); err != nil {
		r.fail(fmt.Errorf("restart fenced Execution service for the same incarnation: %w", err))
	}
}

func (r *Runtime) facts() (*environmentruntimestatepb.EnvironmentRuntimeFacts, error) {
	r.mu.Lock()
	var facts *environmentruntimestatepb.EnvironmentRuntimeFacts
	hostReady := false
	intentPresent := r.host != nil && r.intent != nil && r.host.report != nil && proto.Equal(r.host.report.Binding, r.intent.Binding)
	if intentPresent {
		hostReady = r.host.report.Ready
	}
	if hostReady && r.host.report.Facts != nil {
		facts = proto.Clone(r.host.report.Facts).(*environmentruntimestatepb.EnvironmentRuntimeFacts)
	}
	desired, closing := r.desired, r.closing
	activeAdmissions := r.activeAdmissions
	activeExecution := r.activeExecutionCalls
	executionRecovering := r.executionRecovering
	r.mu.Unlock()
	if facts == nil {
		facts = &environmentruntimestatepb.EnvironmentRuntimeFacts{}
	}
	bootstrapActive := false
	if r.bootstrapHost != nil && r.bootstrapHost.Configured() {
		input, inputFound, err := bootstrap.ReadInput(context.Background(), r.lifecycleState)
		if err != nil {
			return nil, err
		}
		bootstrapState, runtimeFound, err := bootstrap.ReadRuntime(context.Background(), r.lifecycleState)
		if err != nil {
			return nil, err
		}
		bootstrapActive = bootstrapIsActive(input, inputFound, bootstrapState, runtimeFound)
		if bootstrapActive {
			// A requested input is busy even before the lifecycle controller has
			// published Running. Do not expose its idle Host facts in that gap.
			facts.Bootstrap = environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_RUNNING
			if !hasAutosuspendReason(facts.AutosuspendBlockers, "Bootstrap") {
				facts.AutosuspendBlockers = append(facts.AutosuspendBlockers, &sandboxpb.AutosuspendBlocker{Reason: "Bootstrap"})
			}
		} else if runtimeFound {
			switch bootstrapState.Phase {
			case bootstrap.Succeeded:
				facts.Bootstrap = environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_SUCCEEDED
			case bootstrap.Failed:
				facts.Bootstrap = environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_FAILED
			case bootstrap.Interrupted:
				facts.Bootstrap = environmentruntimestatepb.EnvironmentRuntimeFacts_BOOTSTRAP_INTERRUPTED
			}
		}
	}
	if (closing || desired.Mode == lifecycle.Closing || desired.Mode == lifecycle.Quiescing) && !hostReady {
		facts.QuiesceStage = environmentruntimestatepb.EnvironmentRuntimeFacts_QUIESCE_STAGE_DRAINING
	}
	if activeAdmissions > 0 && !hasAutosuspendReason(facts.AutosuspendBlockers, "Admission") {
		facts.AutosuspendBlockers = append(facts.AutosuspendBlockers, &sandboxpb.AutosuspendBlocker{Reason: "Admission"})
	}
	if activeExecution > 0 && !hasAutosuspendReason(facts.AutosuspendBlockers, "ExecutorRelay") {
		facts.AutosuspendBlockers = append(facts.AutosuspendBlockers, &sandboxpb.AutosuspendBlocker{Reason: "ExecutorRelay"})
	}
	if executionRecovering && !hasAutosuspendReason(facts.AutosuspendBlockers, "ExecutorRelay") {
		facts.AutosuspendBlockers = append(facts.AutosuspendBlockers, &sandboxpb.AutosuspendBlocker{Reason: "ExecutorRelay"})
	}
	if activeAdmissions > 0 || activeExecution > 0 || executionRecovering || bootstrapActive || r.state.Lameduck() || desired.Mode != lifecycle.Running {
		facts.IdleSince = nil
	} else if desired.Mode == lifecycle.Running {
		// The Host's idle timestamp is valid only while the active environment binding matches.
		if !intentPresent || !hostReady {
			facts.IdleSince = nil
		}
	}
	sort.Slice(facts.AutosuspendBlockers, func(i, j int) bool {
		return facts.AutosuspendBlockers[i].GetReason() < facts.AutosuspendBlockers[j].GetReason()
	})
	return facts, nil
}

func hasAutosuspendReason(blockers []*sandboxpb.AutosuspendBlocker, reason string) bool {
	for _, blocker := range blockers {
		if blocker.GetReason() == reason {
			return true
		}
	}
	return false
}

func bootstrapIsActive(input bootstrap.Input, inputFound bool, state bootstrap.Runtime, stateFound bool) bool {
	terminal := stateFound && (state.Phase == bootstrap.Succeeded || state.Phase == bootstrap.Failed || state.Phase == bootstrap.Interrupted)
	return (stateFound && state.Phase == bootstrap.Running) || (inputFound && input.Requested && !terminal)
}

func (r *Runtime) Quiesce(ctx context.Context, request *sessionpb.QuiesceRequest) (*sessionpb.QuiesceResponse, error) {
	if err := ctx.Err(); err != nil {
		return nil, status.FromContextError(err).Err()
	}
	r.transitionMu.Lock()
	defer r.transitionMu.Unlock()
	facts, err := r.facts()
	if err != nil {
		return nil, err
	}
	r.mu.Lock()
	if r.closing {
		r.mu.Unlock()
		return nil, errors.New("environment Runtime is closing")
	}
	if !r.state.Lameduck() && request != nil && request.IfIdleSince != nil && (facts.IdleSince == nil || facts.IdleSince.AsTime().UnixMicro() != request.IfIdleSince.AsTime().UnixMicro()) {
		r.mu.Unlock()
		return nil, status.Error(codes.Aborted, "the Sandbox is busy or its idle_since differs")
	}
	if r.host == nil || !r.host.connected {
		r.mu.Unlock()
		return nil, status.Error(codes.Unavailable, "Harness Host lifecycle connection is unavailable")
	}
	if err := r.state.EnterLameduck(); err != nil {
		r.mu.Unlock()
		return nil, status.Errorf(codes.Unavailable, "persist environment seal: %v", err)
	}
	uid, err := r.state.RequireIncarnationUID()
	if err != nil {
		r.mu.Unlock()
		return nil, status.Errorf(codes.FailedPrecondition, "%v", err)
	}
	if r.desired.Mode == lifecycle.Quiescing && r.desired.SealUID == uid &&
		r.intent != nil && r.intent.Mode == runtimepb.LifecycleIntent_QUIESCING && r.intent.SealUid == uid {
		host := r.host
		binding := proto.Clone(r.intent.Binding).(*runtimepb.Binding)
		currentReport := host.report != nil && host.report.Ready && proto.Equal(host.report.Binding, binding)
		r.mu.Unlock()
		// Repeated controller quiesce calls are normally idempotent. Preserve the
		// old retry behavior only when the Host's current lifecycle generation has
		// recorded an error; issuing a fresh generation while work is still draining
		// invalidates its native-stop and Execution-fence acknowledgements.
		retry := false
		if currentReport {
			hostStatus, statusErr := host.client.GetStatus(r.hostContext(ctx, binding), &sessionpb.GetStatusRequest{})
			if statusErr != nil {
				return nil, status.Errorf(codes.Unavailable, "read Harness Host quiesce status: %v", statusErr)
			}
			retry = hostStatus.GetLifecycle().GetError() != ""
		}
		if !retry {
			facts, err = r.facts()
			if err != nil {
				return nil, err
			}
			return &sessionpb.QuiesceResponse{Stage: sessionpb.QuiesceStage(facts.QuiesceStage)}, nil
		}
		r.mu.Lock()
		if r.closing {
			r.mu.Unlock()
			return nil, errors.New("environment Runtime is closing")
		}
		if r.host != host || r.intent == nil || !proto.Equal(r.intent.Binding, binding) ||
			r.desired.Mode != lifecycle.Quiescing || r.desired.SealUID != uid {
			r.mu.Unlock()
			facts, err = r.facts()
			if err != nil {
				return nil, err
			}
			return &sessionpb.QuiesceResponse{Stage: sessionpb.QuiesceStage(facts.QuiesceStage)}, nil
		}
	}
	r.desired = lifecycle.Desired{Mode: lifecycle.Quiescing, SealUID: uid, Generation: r.desired.Generation + 1}
	if err := lifecycle.PublishDesired(context.Background(), r.lifecycleState, r.desired); err != nil {
		r.mu.Unlock()
		return nil, status.Errorf(codes.Unavailable, "publish environment quiesce: %v", err)
	}
	r.setIntentLocked(runtimepb.LifecycleIntent_QUIESCING, uid, uid, false)
	r.mu.Unlock()
	facts, err = r.facts()
	if err != nil {
		return nil, err
	}
	return &sessionpb.QuiesceResponse{Stage: sessionpb.QuiesceStage(facts.QuiesceStage)}, nil
}

func (r *Runtime) isClosing() bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.closing
}

func (r *Runtime) changesSince() <-chan struct{} {
	r.mu.Lock()
	defer r.mu.Unlock()
	return r.changes
}

func (r *Runtime) changedLocked() {
	close(r.changes)
	r.changes = make(chan struct{})
}

func (r *Runtime) intentChangedLocked() {
	if r.host != nil {
		close(r.host.intentChanged)
		r.host.intentChanged = make(chan struct{})
	}
	r.changedLocked()
}

func (r *Runtime) fail(err error) {
	if err == nil {
		return
	}
	r.mu.Lock()
	if r.failureErr == nil {
		r.failureErr = err
	}
	r.failed = true
	r.changedLocked()
	r.mu.Unlock()
	select {
	case r.fatal <- err:
	default:
	}
}

func (r *Runtime) failureLocked() error {
	if r.failureErr != nil {
		return r.failureErr
	}
	if r.failed {
		return errors.New("environment Runtime failed")
	}
	return nil
}

func (r *Runtime) closeOwner() error { return r.owner.Close() }

func dialUnix(path string) (*grpc.ClientConn, error) {
	return grpc.NewClient("unix://"+path, observability.GRPCClientOption(), grpc.WithTransportCredentials(insecure.NewCredentials()))
}

var (
	_ environmentruntimepb.EnvironmentRuntimeServer = (*Runtime)(nil)
	_ runtimepb.EnvironmentRuntimeServer            = (*Runtime)(nil)
)
