package runner

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"net"
	"net/http"
	"net/url"
	"path/filepath"
	"strconv"
	"sync"
	"time"

	"github.com/coder/websocket"
	"github.com/sethvargo/go-retry"
	"go.uber.org/zap"
)

// A nil executor config selects Codex's local execution. ContainerSocket selects
// the container companion; otherwise Runner supervises a separate cgroup leaf.
type CodexExecServerConfig struct {
	URL             string
	ContainerSocket string
	Isolation       *AgentCgroups
}

func (c CodexExecServerConfig) validate(isolation *AgentCgroups) error {
	if err := validateExecutorURL(c.URL); err != nil {
		return err
	}
	if c.ContainerSocket != "" {
		if !filepath.IsAbs(c.ContainerSocket) || c.Isolation != nil || isolation != nil {
			return errors.New("container executor requires an absolute control socket and container-owned limits")
		}
		return nil
	}
	if isolation == nil || c.Isolation == nil {
		return errors.New("supervised executor requires cgroup isolation and explicit leaf limits")
	}
	if filepath.Dir(c.Isolation.Root) != filepath.Dir(isolation.Root) || c.Isolation.Root == isolation.Root {
		return errors.New("executor and harness cgroups must be distinct sibling subtrees")
	}
	// Validate both policies before Prepare can change either subtree.
	for _, groups := range []*AgentCgroups{isolation, c.Isolation} {
		if err := groups.AggregateLimits.Validate(); err != nil {
			return err
		}
		if err := groups.validateProcessLimits(groups.ProcessLimits); err != nil {
			return err
		}
	}
	return nil
}

func validateExecutorURL(value string) error {
	u, err := url.Parse(value)
	if err != nil {
		return err
	}
	port, err := strconv.Atoi(u.Port())
	if err != nil || port < 1 || port > 65535 || u.Scheme != "ws" || u.Hostname() != "127.0.0.1" || u.User != nil || u.Path != "" || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" {
		return errors.New("executor URL must be ws://127.0.0.1:<nonzero port>")
	}
	return nil
}

type codexExecServer struct {
	runner *Runner
	config CodexExecServerConfig
	mu     sync.Mutex

	instance           *codexExecServerInstance
	instanceGeneration uint64
	startOp            *operation
	probeOp            *operation
	fenceOp            *operation
	startEffect        func(context.Context) (*codexExecServerInstance, error)
	probeEffect        func(context.Context) error
}

func newCodexExecServer(r *Runner, config CodexExecServerConfig) *codexExecServer {
	return &codexExecServer{runner: r, config: config}
}

// request publishes demand under the Runner admission barrier. COSI converts
// accepted demand into a process.
func (e *codexExecServer) request() error {
	e.runner.mu.Lock()
	defer e.runner.mu.Unlock()

	return e.requestLocked()
}

// requestLocked publishes demand while the caller holds Runner.mu, the
// synchronous admission barrier shared with Close and Quiesce.
func (e *codexExecServer) requestLocked() error {
	l := e.runner.lifecycle
	if l == nil {
		return errors.New("Codex exec-server lifecycle is not initialized")
	}
	if !l.accepting() {
		return errors.New("runner is not accepting new Codex exec-server demand")
	}
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	input, found, err := lifecycleRead[codexExecServerInputSpec](e.runner.ctx, l.state, codexExecServerResourceID)
	if err != nil {
		return err
	}
	if !found {
		return errors.New("Codex exec-server input is not initialized")
	}
	input.Demand++
	return publishLifecycle(l, codexExecServerResourceID, input)
}

func (e *codexExecServer) publishExit(generation uint64, err error) {
	l := e.runner.lifecycle
	if l == nil {
		e.runner.fail(errors.New("Codex exec-server exited before lifecycle initialization"))
		return
	}
	l.publishMu.Lock()
	defer l.publishMu.Unlock()
	input, found, readErr := lifecycleRead[codexExecServerInputSpec](e.runner.ctx, l.state, codexExecServerResourceID)
	if readErr != nil {
		e.runner.fail(readErr)
		return
	}
	if !found {
		e.runner.fail(errors.New("Codex exec-server input disappeared"))
		return
	}
	input.ExitGeneration = generation
	if err != nil {
		input.ExitError = err.Error()
	}
	e.runner.fail(publishLifecycle(l, codexExecServerResourceID, input))
}

func (e *codexExecServer) observe(instance *codexExecServerInstance, generation uint64) {
	e.runner.lifecycle.workers.Add(1)
	go func() {
		defer e.runner.lifecycle.workers.Done()
		err := <-instance.done
		e.mu.Lock()
		current := e.instance == instance && e.instanceGeneration == generation
		e.mu.Unlock()
		if current {
			// The observer only publishes process-exit evidence. Reconciliation owns fencing
			// and whether a successor is allowed.
			e.publishExit(generation, err)
		}
	}()
}

func (e *codexExecServer) install(instance *codexExecServerInstance, generation uint64) {
	e.mu.Lock()
	e.instance, e.instanceGeneration = instance, generation
	e.mu.Unlock()
	e.observe(instance, generation)
}

func (e *codexExecServer) currentInstance() (*codexExecServerInstance, uint64) {
	e.mu.Lock()
	defer e.mu.Unlock()
	return e.instance, e.instanceGeneration
}

func (e *codexExecServer) clearInstance(instance *codexExecServerInstance) {
	e.mu.Lock()
	if e.instance == instance {
		e.instance, e.instanceGeneration = nil, 0
	}
	e.mu.Unlock()
}

type codexExecServerInstance struct {
	done <-chan error
	stop func(context.Context) error
}

func (e *codexExecServer) start(ctx context.Context) (*codexExecServerInstance, error) {
	env := maps.Clone(e.runner.config.HarnessEnvironment)
	// The executor needs tool routing and identity, not model authentication.
	for _, name := range []string{"OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_BASE_URL", "CODEX_HOME"} {
		delete(env, name)
	}
	if e.config.ContainerSocket != "" {
		return claimCodexExecServerContainer(ctx, e.config.ContainerSocket, env)
	}
	p, err := NewProcess(ProcessConfig{
		Command: []string{e.runner.config.Codex.Binary, "exec-server", "--listen", e.config.URL},
		Dir:     e.runner.config.StateDir, Env: env, StateOwnerFD: e.runner.owner.File(),
		ProcessIsolation: e.config.Isolation,
	})
	if err != nil {
		return nil, err
	}
	if err := p.Start(ctx); err != nil {
		return nil, err
	}
	go func() { _, _ = io.Copy(io.Discard, p.Stdout()) }()
	go func() {
		// Preserve supervisor OOM evidence and Codex diagnostics on Runner stderr.
		buffer := make([]byte, 4096)
		for {
			n, err := p.Stderr().Read(buffer)
			if n > 0 {
				zap.L().Info("Codex exec-server stderr", zap.String("text", string(buffer[:n])))
			}
			if err != nil {
				return
			}
		}
	}()
	done := make(chan error, 1)
	go func() {
		code, err := p.Wait(context.Background())
		done <- errors.Join(fmt.Errorf("Codex exec-server exited: code=%d; %s", code, p.ExitDescription()), err)
	}()
	return &codexExecServerInstance{done: done, stop: func(ctx context.Context) error { return p.Stop(ctx, stopGrace) }}, nil
}

func (e *codexExecServer) startProcess(ctx context.Context) (*codexExecServerInstance, error) {
	if e.startEffect != nil {
		return e.startEffect(ctx)
	}
	return e.start(ctx)
}

func (e *codexExecServer) probe(ctx context.Context) error {
	if e.probeEffect != nil {
		return e.probeEffect(ctx)
	}
	return probeCodexExecServer(ctx, e.config.URL)
}

// Probe the actual exec-server dispatcher, with a deadline on both handshake and
// response. A listening socket or /readyz alone does not demonstrate RPC progress.
func probeCodexExecServer(ctx context.Context, endpoint string) error {
	c, _, err := websocket.Dial(ctx, endpoint, &websocket.DialOptions{HTTPClient: &http.Client{Transport: &http.Transport{Proxy: nil}}})
	if err != nil {
		return err
	}
	defer c.CloseNow()
	c.SetReadLimit(65536)
	if err := c.Write(ctx, websocket.MessageText, []byte(`{"id":1,"method":"initialize","params":{"clientName":"agentplane-health"}}`)); err != nil {
		return err
	}
	_, data, err := c.Read(ctx)
	if err != nil {
		return err
	}
	var response struct {
		ID     int `json:"id"`
		Result struct {
			SessionID string `json:"sessionId"`
		} `json:"result"`
	}
	if err := json.Unmarshal(data, &response); err != nil {
		return err
	}
	if response.ID != 1 || response.Result.SessionID == "" {
		return errors.New("executor health initialize returned no session")
	}
	if err := c.Write(ctx, websocket.MessageText, []byte(`{"method":"initialized","params":null}`)); err != nil {
		return err
	}
	if err := c.Write(ctx, websocket.MessageText, []byte(`{"id":2,"method":"environment/status","params":null}`)); err != nil {
		return err
	}
	_, data, err = c.Read(ctx)
	if err != nil {
		return err
	}
	var status struct {
		ID     int `json:"id"`
		Result struct {
			Status string `json:"status"`
		} `json:"result"`
	}
	if err := json.Unmarshal(data, &status); err != nil {
		return err
	}
	if status.ID != 2 || status.Result.Status != "ready" {
		return errors.New("executor did not report ready status")
	}
	return nil
}

// codexExecServerRefusal is a companion's refusal to start Codex at all, with its reason. It describes a
// generation that spawned nothing, so it needs no fencing and repeats until the container is replaced.
type codexExecServerRefusal struct{ reason string }

func (r codexExecServerRefusal) Error() string {
	return "executor companion refused to start Codex: " + r.reason
}

// All companion control traffic is local to a Pod over an emptyDir Unix socket.
// A generation identifies a whole container PID namespace, not just exec-server.
type codexExecServerControlRequest struct {
	Operation   string            `json:"operation"`
	Environment map[string]string `json:"environment,omitempty"`
}
type codexExecServerControlStatus struct {
	Generation string `json:"generation"`
	Owned      bool   `json:"owned"`
	// Error explains a claim that was refused without starting anything, so the reason survives
	// the wire. Empty otherwise. A refusal never ends a container generation: no child was spawned,
	// so there is nothing left in it to fence.
	Error string `json:"error,omitempty"`
}

func codexExecServerControl(ctx context.Context, socket string, request codexExecServerControlRequest) (net.Conn, codexExecServerControlStatus, error) {
	c, err := (&net.Dialer{}).DialContext(ctx, "unix", socket)
	if err != nil {
		return nil, codexExecServerControlStatus{}, err
	}
	stop := context.AfterFunc(ctx, func() { _ = c.Close() })
	defer stop()
	deadline, _ := ctx.Deadline()
	_ = c.SetDeadline(deadline)
	if err := json.NewEncoder(c).Encode(request); err != nil {
		_ = c.Close()
		return nil, codexExecServerControlStatus{}, err
	}
	var status codexExecServerControlStatus
	if err := json.NewDecoder(c).Decode(&status); err != nil {
		_ = c.Close()
		return nil, status, err
	}
	if status.Generation == "" {
		_ = c.Close()
		return nil, status, errors.New("executor companion returned no generation")
	}
	_ = c.SetDeadline(time.Time{})
	return c, status, nil
}

// fenceCodexExecServerContainer waits until a fresh, idle container proves the old PID
// namespace is gone. EOF alone is insufficient: detached descendants can remain
// alive between the companion exiting and the container runtime finishing cleanup.
func fenceCodexExecServerContainer(ctx context.Context, socket, previous string) error {
	return retry.Do(ctx, retry.NewConstant(200*time.Millisecond), func(ctx context.Context) error {
		attempt, cancel := context.WithTimeout(ctx, 2*time.Second)
		defer cancel()
		c, status, err := codexExecServerControl(attempt, socket, codexExecServerControlRequest{Operation: "inspect"})
		if err != nil {
			return retry.RetryableError(err)
		}
		defer c.Close()
		if !status.Owned && status.Generation != previous {
			return nil
		}
		if status.Owned {
			if err := json.NewEncoder(c).Encode(codexExecServerControlRequest{Operation: "fence"}); err != nil {
				return retry.RetryableError(err)
			}
		}
		return retry.RetryableError(errors.New("waiting for executor container cleanup and restart"))
	})
}

func claimCodexExecServerContainer(ctx context.Context, socket string, env map[string]string) (*codexExecServerInstance, error) {
	c, status, err := codexExecServerControl(ctx, socket, codexExecServerControlRequest{Operation: "claim", Environment: env})
	if err != nil {
		return nil, err
	}
	if !status.Owned {
		_ = c.Close()
		if status.Error != "" {
			return nil, codexExecServerRefusal{reason: status.Error}
		}
		return nil, errors.New("executor companion refused ownership")
	}
	done := make(chan error, 1)
	go func() {
		_, err := io.Copy(io.Discard, c)
		done <- errors.Join(errors.New("executor container ownership connection lost"), err)
	}()
	return &codexExecServerInstance{done: done, stop: func(ctx context.Context) error {
		_ = c.Close()
		return fenceCodexExecServerContainer(ctx, socket, status.Generation)
	}}, nil
}
