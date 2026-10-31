package codexappserver

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
	"github.com/agentydragon/ducktape/agentplane/runner/codex"
	"github.com/agentydragon/ducktape/agentplane/runner/codexexecserver"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/agentydragon/ducktape/agentplane/runner/operation"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/runner/lifecycle"
	"github.com/cosi-project/runtime/pkg/controller"
	"github.com/cosi-project/runtime/pkg/state"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"go.uber.org/zap"
)

// Attachment is one Session's stable routing identity for a process incarnation. Callbacks
// stay in the Session owner, which commits frame batches before waking and delivering replies.
type Attachment struct {
	ID     string
	Frames func(context.Context, [][]byte) error
	Stderr func(context.Context, string) error
	End    func(context.Context, int) error
}

type RequestAdmission func(context.Context, func(context.Context) error) error

// Readiness contains the independently configurable app-server startup windows.
type Readiness struct {
	ExecServerDependencyTimeout time.Duration
	StartupTimeout              time.Duration
	InitializeTimeout           time.Duration
}

// Resolve supplies defaults for omitted windows and rejects invalid direct Go configuration.
func (r Readiness) Resolve() (Readiness, error) {
	if err := resolveDuration("Codex exec-server dependency timeout", &r.ExecServerDependencyTimeout, 30*time.Second); err != nil {
		return Readiness{}, err
	}
	if err := resolveDuration("Codex app-server startup timeout", &r.StartupTimeout, 90*time.Second); err != nil {
		return Readiness{}, err
	}
	if err := resolveDuration("Codex app-server initialize timeout", &r.InitializeTimeout, 60*time.Second); err != nil {
		return Readiness{}, err
	}
	return r, nil
}

func resolveDuration(name string, value *time.Duration, defaultValue time.Duration) error {
	if *value < 0 {
		return fmt.Errorf("%s must be positive", name)
	}
	if *value == 0 {
		*value = defaultValue
	}
	return nil
}

type Dependencies struct {
	Context            context.Context
	State              state.State
	Workers            *sync.WaitGroup
	Processes          *supervision.Host
	Supervisor         *supervision.Supervisor
	HarnessEnvironment map[string]string
	StateDir           string
	RequestAdmission   RequestAdmission
	Admission          lifecycle.AdmissionGuard
	ExecServer         *codexexecserver.Host
	Readiness          Readiness
	OnError            func(error)
	OnChange           func()
}

// Host owns the one shared Codex process and its protocol readers.
type Host struct {
	deps                        Dependencies
	launch                      harness.LaunchConfig
	store                       string
	handshakeMu                 sync.Mutex
	inputMu                     sync.Mutex
	mu                          sync.Mutex
	current                     *Process
	initializingOwner           *Attachment
	initializingOwnerGeneration uint64
	initializing                *operation.Operation
	draining                    *operation.Operation
	initializationGeneration    uint64
	drainEffect                 func(context.Context) error
	changeMu                    sync.Mutex
	changed                     chan struct{}
}

// Process is a process-incarnation handle. It never exposes routing or mutable host state.
type Process struct {
	process     *supervision.ManagedProcess
	router      *codex.Router[*Attachment]
	hostingMu   sync.Mutex
	exiting     bool
	initialized chan error
	readers     sync.WaitGroup
	ended       chan struct{}
	readerErrMu sync.Mutex
	readerErr   error
}

func (p *Process) ID() string                           { return p.process.ID() }
func (p *Process) Process() *supervision.ManagedProcess { return p.process }
func (p *Process) PID() int                             { return p.process.PID() }
func (p *Process) Running() bool                        { return p.process.Running() }
func (p *Process) WriteLine(ctx context.Context, frame string) error {
	return p.process.WriteLine(ctx, frame)
}

func (p *Process) RegisterSent(host *Attachment, frame json.RawMessage) error {
	return p.router.Sent(host, frame)
}
func (p *Process) Release(host *Attachment) { p.router.Release(host) }

func (p *Process) host(s *Attachment) bool {
	p.hostingMu.Lock()
	defer p.hostingMu.Unlock()
	if p.exiting || !p.process.Running() {
		return false
	}
	if s != nil {
		p.router.Host(s)
	}
	return true
}

func (p *Process) beginExit() []*Attachment {
	p.hostingMu.Lock()
	defer p.hostingMu.Unlock()
	p.exiting = true
	return p.router.Hosts()
}

func NewHost(launch harness.LaunchConfig, deps Dependencies) (*Host, error) {
	if deps.Context == nil || deps.State == nil || deps.Workers == nil || deps.Processes == nil || deps.Supervisor == nil || deps.StateDir == "" || deps.RequestAdmission == nil || deps.Admission == nil || deps.OnError == nil {
		return nil, errors.New("Codex app-server host dependencies are incomplete")
	}
	readiness, err := deps.Readiness.Resolve()
	if err != nil {
		return nil, err
	}
	deps.Readiness = readiness
	return &Host{deps: deps, launch: launch, store: filepath.Join(deps.StateDir, "codex"), changed: make(chan struct{})}, nil
}

func (c *Host) Controller() controller.QController { return &LifecycleController{host: c} }
func (c *Host) Initialize(ctx context.Context) error {
	return publish(ctx, c.deps.State, ResourceID, Input{})
}

func (c *Host) ResetDemand(ctx context.Context) error {
	c.inputMu.Lock()
	defer c.inputMu.Unlock()
	return publish(ctx, c.deps.State, ResourceID, Input{})
}

func (c *Host) WithThreadHandshake(work func() error) error {
	c.handshakeMu.Lock()
	defer c.handshakeMu.Unlock()
	return work()
}

func (c *Host) notify() {
	c.changeMu.Lock()
	close(c.changed)
	c.changed = make(chan struct{})
	c.changeMu.Unlock()
}

func (c *Host) observe() {
	c.notify()
	if c.deps.OnChange != nil {
		c.deps.OnChange()
	}
}

func (c *Host) waitFor(ctx context.Context, predicate func() (bool, error)) error {
	for {
		c.changeMu.Lock()
		changed := c.changed
		c.changeMu.Unlock()
		ready, err := predicate()
		if ready || err != nil {
			return err
		}
		select {
		case <-changed:
		case <-ctx.Done():
			return ctx.Err()
		case <-c.deps.Context.Done():
			return c.deps.Context.Err()
		}
	}
}

// Host admits demand synchronously under the Runner's admission barrier, then waits for readiness.
func (c *Host) Host(ctx context.Context, s *Attachment) (*Process, error) {
	var demand uint64
	if err := c.deps.RequestAdmission(ctx, func(ctx context.Context) error {
		var err error
		demand, err = c.RequestLocked(ctx, s)
		return err
	}); err != nil {
		return nil, err
	}
	var ready *Process
	err := c.waitFor(ctx, func() (bool, error) {
		desired, _, err := lifecycle.ReadDesired(c.deps.Context, c.deps.State)
		if err != nil {
			return false, err
		}
		if desired.Mode != lifecycle.Running {
			return false, errors.New("runner is not accepting new Codex work")
		}
		runtime, _, err := ReadRuntime(c.deps.Context, c.deps.State)
		if err != nil {
			return false, err
		}
		if runtime.Phase == Ready {
			c.mu.Lock()
			p := c.current
			if p != nil && p.ID() == runtime.ProcessID && p.host(s) {
				ready = p
				c.mu.Unlock()
				return true, nil
			}
			settled := p == nil
			c.mu.Unlock()
			if settled && runtime.Attempt >= demand {
				return false, errors.New("Codex app-server exited before the Session could attach")
			}
		}
		// Initialization errors may be published before cancellation has finished draining a
		// process that was started concurrently with executor readiness. Do not release waiters
		// until the controller has settled that process.
		if runtime.Phase == Stopped && runtime.Attempt >= demand && runtime.Error != "" {
			return false, errors.New(runtime.Error)
		}
		if runtime.Phase == Stopped && runtime.Attempt >= demand {
			return false, errors.New("Codex app-server stopped before the Session could attach")
		}
		return false, nil
	})
	if err != nil {
		return nil, err
	}
	return ready, nil
}

// RequestLocked records accepted demand. Caller holds the synchronous Runner admission barrier.
func (c *Host) RequestLocked(ctx context.Context, s *Attachment) (uint64, error) {
	desired, found, err := lifecycle.ReadDesired(ctx, c.deps.State)
	if err != nil {
		return 0, err
	}
	if !found || desired.Mode != lifecycle.Running {
		return 0, errors.New("runner is not accepting new Codex work")
	}
	c.inputMu.Lock()
	defer c.inputMu.Unlock()
	c.mu.Lock()
	if c.current == nil || !c.current.Running() {
		if c.initializingOwner == nil || c.initializingOwnerGeneration != desired.Generation {
			c.initializingOwner = s
			c.initializingOwnerGeneration = desired.Generation
		}
	}
	c.mu.Unlock()
	input, found, err := ReadInput(ctx, c.deps.State)
	if err != nil {
		c.clearInitializingOwner(s)
		return 0, err
	}
	if !found {
		c.clearInitializingOwner(s)
		return 0, errors.New("Codex input is not initialized")
	}
	input.Demand++
	if err := publish(ctx, c.deps.State, ResourceID, input); err != nil {
		c.clearInitializingOwner(s)
		return 0, err
	}
	c.notify()
	return input.Demand, nil
}

func (c *Host) clearInitializingOwner(owner *Attachment) {
	c.mu.Lock()
	if c.initializingOwner == owner {
		c.initializingOwner = nil
		c.initializingOwnerGeneration = 0
	}
	c.mu.Unlock()
}

func (c *Host) initialize(ctx context.Context, owner *Attachment, generation uint64) (resultErr error) {
	attributes := []attribute.KeyValue{attribute.Int64("agentplane.codex.generation", int64(generation))}
	if owner != nil {
		attributes = append(attributes, attribute.String("session.id", owner.ID))
	}
	ctx, span := observability.Start(ctx, "agentplane.codex.app_server.start", trace.WithAttributes(attributes...))
	defer func() { observability.End(span, resultErr) }()
	startup, cancel := context.WithTimeout(ctx, c.deps.Readiness.StartupTimeout)
	defer cancel()
	command, err := codex.ServerCommand(c.launch)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(c.store, 0o700); err != nil {
		return err
	}
	env := maps.Clone(c.deps.HarnessEnvironment)
	if env == nil {
		env = map[string]string{}
	}
	maps.Copy(env, codex.ServerEnvironment(c.launch, c.store))
	if c.deps.ExecServer != nil {
		env["CODEX_EXEC_SERVER_URL"] = c.deps.ExecServer.URL()
	}
	initialize, initialized, err := codex.InitializeFrames()
	if err != nil {
		return err
	}
	launchCtx, launchSpan := observability.Start(startup, "agentplane.codex.app_server.launch")
	process, err := c.deps.Processes.Launch(launchCtx, ControllerName, generation, c.deps.Supervisor, supervision.ProcessConfig{Command: command, Dir: c.store, Env: env})
	observability.End(launchSpan, err)
	if err != nil {
		return err
	}
	p := &Process{
		process: process, router: codex.NewRouter[*Attachment](),
		initialized: make(chan error, 1), ended: make(chan struct{}),
	}
	if owner != nil {
		p.router.Host(owner)
	}
	c.mu.Lock()
	c.current = p
	c.mu.Unlock()
	p.readers.Add(2)
	go c.readStdout(p)
	go c.readStderr(p)
	go c.waitReaders(p)
	c.notify()
	initializeCtx, initializeSpan := observability.Start(startup, "agentplane.codex.app_server.initialize")
	err = process.WriteLine(initializeCtx, string(initialize))
	if err == nil {
		timer := time.NewTimer(c.deps.Readiness.InitializeTimeout)
		select {
		case err = <-p.initialized:
		case <-p.ended:
			err = errors.New("the Codex app-server exited before answering initialize")
		case <-startup.Done():
			err = startup.Err()
		case <-timer.C:
			err = fmt.Errorf("the Codex app-server did not answer initialize within %s", c.deps.Readiness.InitializeTimeout)
		}
		timer.Stop()
	}
	if err == nil {
		err = process.WriteLine(initializeCtx, string(initialized))
	}
	observability.End(initializeSpan, err)
	if err != nil {
		stopErr := process.Stop(context.WithoutCancel(startup))
		if stopErr != nil {
			return errors.Join(fmt.Errorf("Codex app-server startup failed: %w; %s", err, process.ExitDescription()), stopErr, p.readError())
		}
		<-p.ended
		return errors.Join(fmt.Errorf("Codex app-server startup failed: %w; %s", err, process.ExitDescription()), p.readError())
	}
	return nil
}

// waitReaders closes ended only after stdout has journaled every hosted Session's exit and stderr has
// delivered its remaining diagnostics. The app lifecycle controller waits for this before fencing Runner.
func (c *Host) waitReaders(p *Process) {
	p.readers.Wait()
	cleanupErr := p.process.WaitStopped(c.deps.Context)
	if cleanupErr != nil {
		p.recordReadError(cleanupErr)
		c.deps.OnError(cleanupErr)
	}
	c.mu.Lock()
	if c.current == p && cleanupErr == nil {
		c.current = nil
	}
	close(p.ended)
	c.mu.Unlock()
	c.publishExit(p)
	c.observe()
}

func (c *Host) publishExit(p *Process) {
	c.inputMu.Lock()
	defer c.inputMu.Unlock()
	input, found, err := ReadInput(c.deps.Context, c.deps.State)
	if err == nil && !found {
		err = errors.New("Codex input disappeared before process exit was observed")
	}
	if err == nil {
		input.ExitGeneration++
		input.ExitProcessID = p.process.ID()
		if readerErr := p.readError(); readerErr != nil {
			input.ExitError = readerErr.Error()
		} else {
			input.ExitError = ""
		}
		err = publish(c.deps.Context, c.deps.State, ResourceID, input)
	}
	if err != nil {
		c.deps.OnError(err)
	}
}

func (p *Process) recordReadError(err error) {
	if err == nil {
		return
	}
	p.readerErrMu.Lock()
	p.readerErr = errors.Join(p.readerErr, err)
	p.readerErrMu.Unlock()
}

func (p *Process) readError() error {
	p.readerErrMu.Lock()
	defer p.readerErrMu.Unlock()
	return p.readerErr
}

// readStdout journals each frame in the Sessions it concerns. Frames of one Session keep their order;
// each read's frames are committed in one batch per Session.
func (c *Host) readStdout(p *Process) {
	defer p.readers.Done()
	buffer := make([]byte, supervision.ReadBatchBytes)
	var framer harness.StdoutFramer
	var readErr error
	initializing := true
	route := func(lines [][]byte) error {
		var order []*Attachment
		routed := map[*Attachment][][]byte{}
		for _, line := range lines {
			if initializing {
				if answered, err := codex.InitializeResponse(line); answered {
					initializing = false
					p.initialized <- err
					continue
				}
			}
			for _, s := range p.router.Route(line) {
				if _, seen := routed[s]; !seen {
					order = append(order, s)
				}
				routed[s] = append(routed[s], line)
			}
		}
		for _, s := range order {
			if err := s.Frames(c.deps.Context, routed[s]); err != nil {
				return err
			}
		}
		return nil
	}
	for {
		n, err := p.process.Stdout().Read(buffer)
		if n > 0 {
			batch := framer.Feed(buffer[:n])
			if batch.Err != nil && !batch.PendingTooLong {
				readErr = batch.Err
			} else {
				readErr = route(batch.Lines)
				if readErr == nil {
					readErr = batch.Err
				}
			}
			if readErr != nil {
				break
			}
		}
		if err != nil {
			if err == io.EOF {
				lines := framer.Finish()
				if len(lines) > 0 {
					readErr = route(lines)
				}
			}
			if err != io.EOF && readErr == nil {
				readErr = err
			}
			break
		}
	}
	if readErr != nil {
		p.recordReadError(readErr)
		c.deps.OnError(readErr)
		if err := p.process.Stop(context.Background()); err != nil {
			p.recordReadError(err)
			c.deps.OnError(err)
		}
	}
	code, err := p.process.Wait(c.deps.Context)
	if err != nil {
		p.recordReadError(err)
		c.deps.OnError(err)
	}
	for _, s := range p.beginExit() {
		if err := s.End(c.deps.Context, code); err != nil {
			p.recordReadError(err)
			c.deps.OnError(err)
		}
	}
}

// readStderr records the app-server's diagnostics in every hosted Session: none of it names a thread.
func (c *Host) readStderr(p *Process) {
	defer p.readers.Done()
	buffer := make([]byte, 4096)
	for {
		n, err := p.process.Stderr().Read(buffer)
		if n > 0 {
			text := string(bytes.ToValidUTF8(buffer[:n], []byte("�")))
			hosts := p.router.Hosts()
			if len(hosts) == 0 {
				zap.L().Info("Codex app-server stderr", zap.String("process", p.process.ID()), zap.String("text", text))
			}
			for _, s := range hosts {
				if e := s.Stderr(c.deps.Context, text); e != nil {
					p.recordReadError(e)
					c.deps.OnError(e)
					return
				}
			}
		}
		if err != nil {
			if err != io.EOF {
				p.recordReadError(err)
				c.deps.OnError(err)
			}
			return
		}
	}
}

// drainProcess requests COSI to stop the process, then drains its protocol readers before the app-server is settled.
func (c *Host) drainProcess(ctx context.Context) error {
	c.mu.Lock()
	effect := c.drainEffect
	p := c.current
	c.mu.Unlock()
	if effect != nil {
		return effect(ctx)
	}
	if p == nil {
		return nil
	}
	err := p.process.Stop(ctx)
	if err != nil {
		return errors.Join(err, p.readError())
	}
	<-p.ended
	if p.process.Running() {
		return errors.Join(p.readError(), errors.New("Codex app-server remained running after its readers exited"))
	}
	// A native process may report a nonzero exit code after SIGTERM. Once Wait and both readers settle,
	// that exit status does not mean the Runner failed to fence the process.
	return p.readError()
}

// Status describes the running app-server and the Sessions it hosts.
func (c *Host) Status() *runnerpb.HarnessProcessStatus {
	runtime, _, err := ReadRuntime(c.deps.Context, c.deps.State)
	if err != nil {
		c.deps.OnError(err)
		return nil
	}
	if runtime.Phase == Stopped || runtime.ProcessID == "" {
		return nil
	}
	c.mu.Lock()
	p := c.current
	c.mu.Unlock()
	state := runnerpb.ProcessState_PROCESS_STATE_STARTING
	switch runtime.Phase {
	case Ready:
		state = runnerpb.ProcessState_PROCESS_STATE_RUNNING
	case Draining:
		state = runnerpb.ProcessState_PROCESS_STATE_STOPPING
	}
	status := &runnerpb.HarnessProcessStatus{ProcessId: runtime.ProcessID, Harness: runnerpb.Harness_HARNESS_CODEX, State: state}
	if p != nil && p.process.ID() == runtime.ProcessID {
		for _, s := range p.router.Hosts() {
			status.AssignedSessionIds = append(status.AssignedSessionIds, s.ID)
		}
	}
	slices.Sort(status.AssignedSessionIds)
	if p != nil && p.process.ID() == runtime.ProcessID && p.process.Running() {
		pid := int64(p.process.PID())
		status.Pid = &pid
	}
	return status
}
