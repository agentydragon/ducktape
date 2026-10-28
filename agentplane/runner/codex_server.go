package runner

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"log/slog"
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

	mu       sync.Mutex
	current  *codexProcess
	starting chan struct{}
}

// codexProcess is one incarnation of the shared app-server.
type codexProcess struct {
	id      string
	process *Process
	router  *codex.Router[*Session]
	// state is guarded by codexServer.mu.
	state       runnerpb.ProcessState
	initialized chan error
	readers     sync.WaitGroup
	// ended is closed once every hosted Session has recorded the exit.
	ended chan struct{}
}

func newCodexServer(r *Runner, launch harness.LaunchConfig) *codexServer {
	return &codexServer{runner: r, launch: launch, store: filepath.Join(r.config.StateDir, "codex")}
}

// host returns the running app-server with `s` among its hosted Sessions, starting one if none runs.
// Concurrent callers share one start. The Session that starts it is hosted from the first output, so it
// records what the app-server writes before answering initialize, and the exit of a failed start.
func (c *codexServer) host(ctx context.Context, s *Session) (*codexProcess, error) {
	for {
		c.mu.Lock()
		current, starting := c.current, c.starting
		if current != nil && current.state == runnerpb.ProcessState_PROCESS_STATE_RUNNING && current.process.Running() {
			current.router.Host(s)
			c.mu.Unlock()
			return current, nil
		}
		var wait <-chan struct{}
		switch {
		case current != nil:
			// Exiting or stopping: its Sessions must record the exit before a successor starts.
			wait = current.ended
		case starting != nil:
			wait = starting
		default:
			started := make(chan struct{})
			c.starting = started
			c.mu.Unlock()
			err := c.start(ctx, s)
			c.mu.Lock()
			c.starting = nil
			close(started)
			c.mu.Unlock()
			if err != nil {
				return nil, err
			}
			continue
		}
		c.mu.Unlock()
		select {
		case <-wait:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
	}
}

func (c *codexServer) start(ctx context.Context, s *Session) error {
	if c.runner.executor != nil {
		startup, cancel := context.WithTimeout(ctx, 30*time.Second)
		err := c.runner.executor.ensure(startup)
		cancel()
		if err != nil {
			return fmt.Errorf("Codex executor startup: %w", err)
		}
	}
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
	if err := process.Start(ctx); err != nil {
		return err
	}
	p := &codexProcess{
		id: uuid.NewString(), process: process, router: codex.NewRouter[*Session](),
		state: runnerpb.ProcessState_PROCESS_STATE_STARTING, initialized: make(chan error, 1), ended: make(chan struct{}),
	}
	p.router.Host(s)
	p.readers.Add(2)
	go c.readStdout(p)
	go c.readStderr(p)
	err = process.WriteLine(ctx, string(initialize))
	if err == nil {
		timer := time.NewTimer(60 * time.Second)
		select {
		case err = <-p.initialized:
		case <-p.ended:
			err = errors.New("the Codex app-server exited before answering initialize")
		case <-ctx.Done():
			err = ctx.Err()
		case <-timer.C:
			err = errors.New("the Codex app-server did not answer initialize within 60s")
		}
		timer.Stop()
	}
	if err == nil {
		err = process.WriteLine(ctx, string(initialized))
	}
	if err != nil {
		s.mu.Lock()
		s.nativeStop = stopStartupFailure
		s.processState = runnerpb.ProcessState_PROCESS_STATE_STOPPING
		s.mu.Unlock()
		stopErr := process.Stop(context.WithoutCancel(ctx), 5*time.Second)
		p.readers.Wait()
		return errors.Join(fmt.Errorf("Codex app-server startup failed: %w; %s", err, process.ExitDescription()), stopErr)
	}
	c.mu.Lock()
	p.state = runnerpb.ProcessState_PROCESS_STATE_RUNNING
	c.current = p
	c.mu.Unlock()
	c.runner.changed()
	return nil
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
		c.runner.fail(readErr)
		if err := p.process.Stop(context.Background(), 5*time.Second); err != nil {
			c.runner.fail(err)
		}
	}
	code, err := p.process.Wait(context.Background())
	if err != nil {
		c.runner.fail(err)
	}
	for _, s := range p.router.Hosts() {
		if err := s.harnessEnded(c.runner.ctx, code); err != nil {
			c.runner.fail(err)
		}
	}
	c.mu.Lock()
	if c.current == p {
		c.current = nil
	}
	close(p.ended)
	c.mu.Unlock()
	c.runner.changed()
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
				slog.Info("Codex app-server stderr", "process", p.id, "text", text)
			}
			for _, s := range hosts {
				if e := s.Emit(c.runner.ctx, observation(&eventpb.HarnessStderr{Text: text}), nil); e != nil {
					c.runner.fail(e)
					return
				}
			}
		}
		if err != nil {
			if err != io.EOF {
				c.runner.fail(err)
			}
			return
		}
	}
}

// stop stops the running app-server once every hosted Session has halted, and returns after each has
// recorded the exit.
func (c *codexServer) stop(ctx context.Context) error {
	c.mu.Lock()
	p := c.current
	if p != nil {
		p.state = runnerpb.ProcessState_PROCESS_STATE_STOPPING
	}
	c.mu.Unlock()
	if p == nil {
		return nil
	}
	err := p.process.Stop(ctx, 5*time.Second)
	p.readers.Wait()
	return err
}

// status describes the running app-server and the Sessions it hosts.
func (c *codexServer) status() *runnerpb.HarnessProcessStatus {
	c.mu.Lock()
	p := c.current
	var state runnerpb.ProcessState
	if p != nil {
		state = p.state
	}
	c.mu.Unlock()
	if p == nil {
		return nil
	}
	status := &runnerpb.HarnessProcessStatus{ProcessId: p.id, Harness: runnerpb.Harness_HARNESS_CODEX, State: state}
	for _, s := range p.router.Hosts() {
		status.AssignedSessionIds = append(status.AssignedSessionIds, s.id)
	}
	slices.Sort(status.AssignedSessionIds)
	if p.process.Running() {
		pid := int64(p.process.PID())
		status.Pid = &pid
	}
	return status
}
