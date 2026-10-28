package runner

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"maps"
	"os"
	"path/filepath"
	"slices"
	"sync"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/codex"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/google/uuid"
	"go.uber.org/zap"
)

// codexServer runs the Runner's one Codex app-server, started when a Codex Session first needs it. Each
// Codex Session it hosts is one native thread, and one exit ends all of their harnesses.
type codexServer struct {
	runner *Runner
	launch harness.LaunchConfig
	// store is the one Codex home of every Codex Session, at `<state_dir>/codex`.
	store string
	// handshakeMu serializes thread creation, so a frame naming a thread not yet known belongs to the one
	// Session creating it.
	handshakeMu sync.Mutex

	mu                   sync.Mutex
	current              *codexProcess
	startOwner           *Session
	startOwnerGeneration uint64
	starting             *sessionOperation
	stopping             *sessionOperation
	startGeneration      uint64
	stopEffect           func(context.Context) error
}

// codexProcess is one incarnation of the shared app-server.
type codexProcess struct {
	id          string
	process     *Process
	router      *codex.Router[*Session]
	hostingMu   sync.Mutex
	exiting     bool
	initialized chan error
	readers     sync.WaitGroup
	// ended is closed once every hosted Session has recorded the exit and both readers have drained.
	ended       chan struct{}
	readerErrMu sync.Mutex
	readerErr   error
}

func (p *codexProcess) host(s *Session) bool {
	p.hostingMu.Lock()
	defer p.hostingMu.Unlock()
	if p.exiting || !p.process.Running() {
		return false
	}
	p.router.Host(s)
	return true
}

func (p *codexProcess) beginExit() []*Session {
	p.hostingMu.Lock()
	defer p.hostingMu.Unlock()
	p.exiting = true
	return p.router.Hosts()
}

func newCodexServer(r *Runner, launch harness.LaunchConfig) *codexServer {
	return &codexServer{runner: r, launch: launch, store: filepath.Join(r.config.StateDir, "codex")}
}

// host publishes demand and waits for the COSI-owned shared app-server to become ready. The first
// demander is captured as the owner of startup frames before it publishes demand.
func (c *codexServer) host(ctx context.Context, s *Session) (*codexProcess, error) {
	demand, err := c.request(s)
	if err != nil {
		return nil, err
	}
	var ready *codexProcess
	err = c.runner.lifecycle.waitFor(ctx, func() (bool, error) {
		goal := c.runner.lifecycle.goal()
		if goal.Mode != runnerRunning {
			return false, errors.New("runner is not accepting new Codex work")
		}
		runtime, _, err := lifecycleRead[codexRuntimeSpec](c.runner.ctx, c.runner.lifecycle.state, codexRuntimeType, "codex")
		if err != nil {
			return false, err
		}
		if runtime.Phase == codexReady {
			c.mu.Lock()
			p := c.current
			if p != nil && p.id == runtime.ProcessID && p.host(s) {
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
		if runtime.Attempt >= demand && runtime.Error != "" {
			return false, errors.New(runtime.Error)
		}
		if runtime.Phase == codexStopped && runtime.Attempt >= demand {
			return false, errors.New("Codex app-server stopped before the Session could attach")
		}
		return false, nil
	})
	if err != nil {
		return nil, err
	}
	return ready, nil
}

// request records a new startup demand while holding the same synchronous gate used by Close and
// Quiesce. The controller rechecks RunnerGoal before it starts the native process.
func (c *codexServer) request(s *Session) (uint64, error) {
	r := c.runner
	r.mu.Lock()
	defer r.mu.Unlock()
	if !r.lifecycle.accepting() {
		return 0, errors.New("runner is not accepting new Codex work")
	}
	goal := r.lifecycle.goal()
	if r.executor != nil {
		if err := r.executor.requestLocked(); err != nil {
			return 0, fmt.Errorf("Codex executor request: %w", err)
		}
	}
	c.mu.Lock()
	if c.current == nil || !c.current.process.Running() {
		if c.startOwner == nil || c.startOwnerGeneration != goal.Generation {
			c.startOwner = s
			c.startOwnerGeneration = goal.Generation
		}
	}
	c.mu.Unlock()
	r.lifecycle.publishMu.Lock()
	defer r.lifecycle.publishMu.Unlock()
	input, found, err := lifecycleRead[codexInputSpec](r.ctx, r.lifecycle.state, codexInputType, "codex")
	if err != nil {
		c.clearStartOwner(s)
		return 0, err
	}
	if !found {
		c.clearStartOwner(s)
		return 0, errors.New("Codex input is not initialized")
	}
	input.Demand++
	if err := publishLifecycle(r.lifecycle, codexInputType, "codex", input); err != nil {
		c.clearStartOwner(s)
		return 0, err
	}
	return input.Demand, nil
}

func (c *codexServer) clearStartOwner(owner *Session) {
	c.mu.Lock()
	if c.startOwner == owner {
		c.startOwner = nil
		c.startOwnerGeneration = 0
	}
	c.mu.Unlock()
}

func (c *codexServer) start(ctx context.Context, owner *Session) error {
	startup, cancel := context.WithTimeout(ctx, 90*time.Second)
	defer cancel()
	command, err := codex.ServerCommand(c.launch)
	if err != nil {
		return err
	}
	if err := os.MkdirAll(c.store, 0o700); err != nil {
		return err
	}
	env := maps.Clone(c.runner.config.HarnessEnvironment)
	if env == nil {
		env = map[string]string{}
	}
	maps.Copy(env, codex.ServerEnvironment(c.launch, c.store))
	if c.runner.executor != nil {
		env["CODEX_EXEC_SERVER_URL"] = c.runner.executor.config.URL
	}
	initialize, initialized, err := codex.InitializeFrames()
	if err != nil {
		return err
	}
	process, err := NewProcess(ProcessConfig{Command: command, Dir: c.store, Env: env, StateOwnerFD: c.runner.owner.File(), ProcessIsolation: c.runner.config.ProcessIsolation})
	if err != nil {
		return err
	}
	p := &codexProcess{
		id: uuid.NewString(), process: process, router: codex.NewRouter[*Session](),
		initialized: make(chan error, 1), ended: make(chan struct{}),
	}
	if owner != nil {
		p.router.Host(owner)
	}
	if err := process.Start(startup); err != nil {
		return err
	}
	c.mu.Lock()
	c.current = p
	c.mu.Unlock()
	p.readers.Add(2)
	go c.readStdout(p)
	go c.readStderr(p)
	go c.waitReaders(p)
	c.runner.changed()
	err = process.WriteLine(startup, string(initialize))
	if err == nil {
		timer := time.NewTimer(60 * time.Second)
		select {
		case err = <-p.initialized:
		case <-p.ended:
			err = errors.New("the Codex app-server exited before answering initialize")
		case <-startup.Done():
			err = startup.Err()
		case <-timer.C:
			err = errors.New("the Codex app-server did not answer initialize within 60s")
		}
		timer.Stop()
	}
	if err == nil {
		err = process.WriteLine(startup, string(initialized))
	}
	if err != nil {
		stopErr := process.Stop(context.WithoutCancel(startup), 5*time.Second)
		<-p.ended
		return errors.Join(fmt.Errorf("Codex app-server startup failed: %w; %s", err, process.ExitDescription()), stopErr, p.readError())
	}
	return nil
}

// waitReaders closes ended only after stdout has journaled every hosted Session's exit and stderr has
// delivered its remaining diagnostics. COSI waits for this observation before replacing the process.
func (c *codexServer) waitReaders(p *codexProcess) {
	p.readers.Wait()
	c.mu.Lock()
	if c.current == p {
		c.current = nil
	}
	close(p.ended)
	c.mu.Unlock()
	c.publishExit(p)
	c.runner.changed()
}

func (c *codexServer) publishExit(p *codexProcess) {
	l := c.runner.lifecycle
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	input, found, err := lifecycleRead[codexInputSpec](c.runner.ctx, l.state, codexInputType, "codex")
	if err == nil && !found {
		err = errors.New("Codex input disappeared before process exit was observed")
	}
	if err == nil {
		input.ExitGeneration++
		input.ExitProcessID = p.id
		if readerErr := p.readError(); readerErr != nil {
			input.ExitError = readerErr.Error()
		} else {
			input.ExitError = ""
		}
		err = publishLifecycle(l, codexInputType, "codex", input)
	}
	if err != nil {
		c.runner.fail(err)
	}
}

func (p *codexProcess) recordReadError(err error) {
	if err == nil {
		return
	}
	p.readerErrMu.Lock()
	p.readerErr = errors.Join(p.readerErr, err)
	p.readerErrMu.Unlock()
}

func (p *codexProcess) readError() error {
	p.readerErrMu.Lock()
	defer p.readerErrMu.Unlock()
	return p.readerErr
}

// readStdout journals each frame in the Sessions it concerns. Frames of one Session keep their order;
// each read's frames are committed in one batch per Session.
func (c *codexServer) readStdout(p *codexProcess) {
	defer p.readers.Done()
	buffer := make([]byte, 64*1024)
	var pending []byte
	var readErr error
	initializing := true
	route := func(lines [][]byte) error {
		var order []*Session
		routed := map[*Session][][]byte{}
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
			adapter := s.hostedAdapter()
			if err := s.journal.WithBatch(c.runner.ctx, func(ctx context.Context) error {
				for _, line := range routed[s] {
					if err := s.receive(ctx, line, adapter); err != nil {
						return err
					}
				}
				return nil
			}); err != nil {
				return err
			}
			s.wake()
			s.deliverReplies()
		}
		return nil
	}
	for {
		n, err := p.process.Stdout().Read(buffer)
		if n > 0 {
			pending = append(pending, buffer[:n]...)
			var lines [][]byte
			for {
				i := bytes.IndexByte(pending, '\n')
				if i < 0 {
					break
				}
				if i > harness.MaxFrameLineBytes {
					readErr = errors.New("native frame exceeded the 64 MiB limit")
					break
				}
				lines = append(lines, append([]byte(nil), bytes.TrimRight(pending[:i], "\r")...))
				pending = pending[i+1:]
			}
			if readErr == nil {
				readErr = route(lines)
			}
			if readErr == nil && len(pending) > harness.MaxFrameLineBytes {
				readErr = errors.New("native frame exceeded the 64 MiB limit")
			}
			if readErr != nil {
				break
			}
		}
		if err != nil {
			if err == io.EOF && len(pending) > 0 {
				readErr = route([][]byte{bytes.TrimRight(pending, "\r")})
			}
			if err != io.EOF && readErr == nil {
				readErr = err
			}
			break
		}
	}
	if readErr != nil {
		p.recordReadError(readErr)
		c.runner.fail(readErr)
		if err := p.process.Stop(context.Background(), 5*time.Second); err != nil {
			p.recordReadError(err)
			c.runner.fail(err)
		}
	}
	code, err := p.process.Wait(context.Background())
	if err != nil {
		p.recordReadError(err)
		c.runner.fail(err)
	}
	for _, s := range p.beginExit() {
		if err := s.harnessEnded(c.runner.ctx, code); err != nil {
			p.recordReadError(err)
			c.runner.fail(err)
		}
	}
}

// readStderr records the app-server's diagnostics in every hosted Session: none of it names a thread.
func (c *codexServer) readStderr(p *codexProcess) {
	defer p.readers.Done()
	buffer := make([]byte, 4096)
	for {
		n, err := p.process.Stderr().Read(buffer)
		if n > 0 {
			text := string(bytes.ToValidUTF8(buffer[:n], []byte("�")))
			hosts := p.router.Hosts()
			if len(hosts) == 0 {
				zap.L().Info("Codex app-server stderr", zap.String("process", p.id), zap.String("text", text))
			}
			for _, s := range hosts {
				if e := s.Emit(c.runner.ctx, observation(&eventpb.HarnessStderr{Text: text}), nil); e != nil {
					p.recordReadError(e)
					c.runner.fail(e)
					return
				}
			}
		}
		if err != nil {
			if err != io.EOF {
				p.recordReadError(err)
				c.runner.fail(err)
			}
			return
		}
	}
}

// stopProcess is the low-level physical stop primitive. The COSI controller owns its policy and phase.
func (c *codexServer) stopProcess(ctx context.Context) error {
	c.mu.Lock()
	effect := c.stopEffect
	p := c.current
	c.mu.Unlock()
	if effect != nil {
		return effect(ctx)
	}
	if p == nil {
		return nil
	}
	err := p.process.Stop(ctx, 5*time.Second)
	<-p.ended
	if p.process.Running() {
		return errors.Join(err, p.readError(), errors.New("Codex app-server remained running after its readers exited"))
	}
	// A native process may report a nonzero exit code after SIGTERM. Once Wait and both readers settle,
	// that exit status does not mean the Runner failed to fence the process.
	return errors.Join(err, p.readError())
}

// status describes the running app-server and the Sessions it hosts.
func (c *codexServer) status() *runnerpb.HarnessProcessStatus {
	if c.runner.lifecycle == nil {
		return nil
	}
	runtime, _, err := lifecycleRead[codexRuntimeSpec](c.runner.ctx, c.runner.lifecycle.state, codexRuntimeType, "codex")
	if err != nil {
		c.runner.fail(err)
		return nil
	}
	if runtime.Phase == codexStopped || runtime.ProcessID == "" {
		return nil
	}
	c.mu.Lock()
	p := c.current
	c.mu.Unlock()
	state := runnerpb.ProcessState_PROCESS_STATE_STARTING
	switch runtime.Phase {
	case codexReady:
		state = runnerpb.ProcessState_PROCESS_STATE_RUNNING
	case codexStopping:
		state = runnerpb.ProcessState_PROCESS_STATE_STOPPING
	}
	status := &runnerpb.HarnessProcessStatus{ProcessId: runtime.ProcessID, Harness: runnerpb.Harness_HARNESS_CODEX, State: state}
	if p != nil && p.id == runtime.ProcessID {
		for _, s := range p.router.Hosts() {
			status.AssignedSessionIds = append(status.AssignedSessionIds, s.id)
		}
	}
	slices.Sort(status.AssignedSessionIds)
	if p != nil && p.id == runtime.ProcessID && p.process.Running() {
		pid := int64(p.process.PID())
		status.Pid = &pid
	}
	return status
}
