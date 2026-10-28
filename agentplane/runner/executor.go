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
type CodexExecutorConfig struct {
	URL             string
	ContainerSocket string
	Isolation       *AgentCgroups
}

func (c CodexExecutorConfig) validate(isolation *AgentCgroups) error {
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

type codexExecutor struct {
	runner                  *Runner
	config                  CodexExecutorConfig
	mu                      sync.Mutex
	started, stopped, ready bool
	changed                 chan struct{}
	done                    chan struct{}
	cancel                  context.CancelFunc
	stopErr                 error
	// refused is the most recent refusal to start Codex, or nil. The companion survives one, so the
	// restart loop keeps trying; this is what a caller whose deadline expired was actually waiting on.
	refused error
}

func newCodexExecutor(r *Runner, config CodexExecutorConfig) *codexExecutor {
	return &codexExecutor{runner: r, config: config, changed: make(chan struct{}), done: make(chan struct{})}
}

func (e *codexExecutor) setReady(ready bool) {
	e.mu.Lock()
	e.ready = ready
	close(e.changed)
	e.changed = make(chan struct{})
	e.mu.Unlock()
}

// setRefusal replaces a companion's refusal to start Codex, waking waiters, and reports whether the
// new value opens an outage rather than repeating one already published. Clearing it (nil) ends the
// outage: an attempt that got elsewhere supersedes the old reason.
func (e *codexExecutor) setRefusal(refusal error) bool {
	e.mu.Lock()
	defer e.mu.Unlock()
	first := refusal != nil && e.refused == nil
	e.refused = refusal
	close(e.changed)
	e.changed = make(chan struct{})
	return first
}

// ensure starts one restart loop for the lifetime of the Runner. It never
// changes execution mode or replays a command after an executor is lost.
func (e *codexExecutor) ensure(ctx context.Context) error {
	e.mu.Lock()
	if !e.started && !e.stopped {
		lifetime, cancel := context.WithCancel(e.runner.ctx)
		e.started, e.cancel = true, cancel
		go e.run(lifetime)
	}
	for {
		if e.stopped {
			e.mu.Unlock()
			return errors.New("Codex executor is stopped")
		}
		if e.ready {
			e.mu.Unlock()
			return nil
		}
		if e.refused != nil {
			refused := e.refused
			e.mu.Unlock()
			return refused
		}
		changed := e.changed
		e.mu.Unlock()
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-e.done:
			return errors.New("Codex executor restart loop stopped")
		case <-changed:
		}
		e.mu.Lock()
	}
}

type executorInstance struct {
	done <-chan error
	stop func(context.Context) error
}

func (e *codexExecutor) start(ctx context.Context) (*executorInstance, error) {
	env := maps.Clone(e.runner.config.HarnessEnvironment)
	// The executor needs tool routing and identity, not model authentication.
	for _, name := range []string{"OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_BASE_URL", "CODEX_HOME"} {
		delete(env, name)
	}
	if e.config.ContainerSocket != "" {
		return claimExecutorContainer(ctx, e.config.ContainerSocket, env)
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
				zap.L().Info("Codex executor stderr", zap.String("text", string(buffer[:n])))
			}
			if err != nil {
				return
			}
		}
	}()
	done := make(chan error, 1)
	go func() {
		code, err := p.Wait(context.Background())
		done <- errors.Join(fmt.Errorf("Codex executor exited: code=%d; %s", code, p.ExitDescription()), err)
	}()
	return &executorInstance{done: done, stop: func(ctx context.Context) error { return p.Stop(ctx, stopGrace) }}, nil
}

func (e *codexExecutor) run(ctx context.Context) {
	defer close(e.done)
	backoff := retry.WithCappedDuration(10*time.Second, retry.NewExponential(time.Second))
	_ = retry.Do(ctx, backoff, func(ctx context.Context) error {
		startCtx, cancel := context.WithTimeout(ctx, 20*time.Second)
		instance, err := e.start(startCtx)
		if err == nil {
			err = retry.Do(startCtx, retry.NewConstant(100*time.Millisecond), func(ctx context.Context) error {
				select {
				case err := <-instance.done:
					return err
				default:
				}
				probeCtx, cancel := context.WithTimeout(ctx, 2*time.Second)
				defer cancel()
				return retry.RetryableError(probeExecutor(probeCtx, e.config.URL))
			})
		}
		cancel()
		var refusal executorRefusal
		if errors.As(err, &refusal) {
			// The companion is serving and will answer the next claim too, so the loop keeps
			// retrying at its backoff. Publish the reason for callers waiting on it, and name it
			// once per outage rather than at every attempt.
			if e.setRefusal(refusal) {
				zap.L().Error("Codex executor refused to start", zap.Error(refusal))
			}
		} else {
			e.setRefusal(nil)
		}
		if err == nil {
			e.setReady(true)
			zap.L().Info("Codex executor ready", zap.String("url", e.config.URL))
			err = monitorExecutor(ctx, e.config.URL, instance.done)
		}
		e.setReady(false)
		if instance != nil {
			cleanupCtx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
			cleanupErr := instance.stop(cleanupCtx)
			cancel()
			if cleanupErr != nil {
				// Fencing failure is fatal: do not serve a successor alongside old work.
				e.mu.Lock()
				e.stopErr = cleanupErr
				e.mu.Unlock()
				e.runner.fail(cleanupErr)
				return cleanupErr
			}
		}
		if ctx.Err() != nil {
			return ctx.Err()
		}
		zap.L().Warn("Codex executor lost; restarting without replay", zap.Error(err))
		return retry.RetryableError(err)
	})
}

func monitorExecutor(ctx context.Context, endpoint string, done <-chan error) error {
	return monitorExecutorHealth(ctx, done, func(ctx context.Context) error { return probeExecutor(ctx, endpoint) })
}

func monitorExecutorHealth(ctx context.Context, done <-chan error, probe func(context.Context) error) error {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	failures := 0
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case err := <-done:
			return err
		case <-ticker.C:
			probeCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
			err := probe(probeCtx)
			cancel()
			if err == nil {
				failures = 0
				continue
			}
			failures++
			// A tool memcg OOM can stall RPC briefly while the kernel selects its victim.
			// Fencing on that first timeout would discard the surviving server's tool result.
			zap.L().Warn("Codex executor health probe failed", zap.Int("consecutive_failures", failures), zap.Error(err))
			if failures >= 3 {
				return fmt.Errorf("executor failed %d consecutive health probes: %w", failures, err)
			}
		}
	}
}

func (e *codexExecutor) stop(ctx context.Context) error {
	e.mu.Lock()
	if !e.stopped {
		e.stopped = true
		close(e.changed)
		e.changed = make(chan struct{})
		if e.started {
			e.cancel()
		} else {
			close(e.done)
		}
	}
	e.mu.Unlock()
	select {
	case <-e.done:
		e.mu.Lock()
		defer e.mu.Unlock()
		return e.stopErr
	case <-ctx.Done():
		return ctx.Err()
	}
}

// Probe the actual exec-server dispatcher, with a deadline on both handshake and
// response. A listening socket or /readyz alone does not demonstrate RPC progress.
func probeExecutor(ctx context.Context, endpoint string) error {
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

// executorRefusal is a companion's refusal to start Codex at all, with its reason. It describes a
// generation that spawned nothing, so it needs no fencing and repeats until the container is replaced.
type executorRefusal struct{ reason string }

func (r executorRefusal) Error() string {
	return "executor companion refused to start Codex: " + r.reason
}

// All companion control traffic is local to a Pod over an emptyDir Unix socket.
// A generation identifies a whole container PID namespace, not just exec-server.
type executorControlRequest struct {
	Operation   string            `json:"operation"`
	Environment map[string]string `json:"environment,omitempty"`
}
type executorControlStatus struct {
	Generation string `json:"generation"`
	Owned      bool   `json:"owned"`
	// Error explains a claim that was refused without starting anything, so the reason survives
	// the wire. Empty otherwise. A refusal never ends a container generation: no child was spawned,
	// so there is nothing left in it to fence.
	Error string `json:"error,omitempty"`
}

func executorControl(ctx context.Context, socket string, request executorControlRequest) (net.Conn, executorControlStatus, error) {
	c, err := (&net.Dialer{}).DialContext(ctx, "unix", socket)
	if err != nil {
		return nil, executorControlStatus{}, err
	}
	stop := context.AfterFunc(ctx, func() { _ = c.Close() })
	defer stop()
	deadline, _ := ctx.Deadline()
	_ = c.SetDeadline(deadline)
	if err := json.NewEncoder(c).Encode(request); err != nil {
		_ = c.Close()
		return nil, executorControlStatus{}, err
	}
	var status executorControlStatus
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

// fenceExecutorContainer waits until a fresh, idle container proves the old PID
// namespace is gone. EOF alone is insufficient: detached descendants can remain
// alive between the companion exiting and the container runtime finishing cleanup.
func fenceExecutorContainer(ctx context.Context, socket, previous string) error {
	return retry.Do(ctx, retry.NewConstant(200*time.Millisecond), func(ctx context.Context) error {
		attempt, cancel := context.WithTimeout(ctx, 2*time.Second)
		defer cancel()
		c, status, err := executorControl(attempt, socket, executorControlRequest{Operation: "inspect"})
		if err != nil {
			return retry.RetryableError(err)
		}
		defer c.Close()
		if !status.Owned && status.Generation != previous {
			return nil
		}
		if status.Owned {
			if err := json.NewEncoder(c).Encode(executorControlRequest{Operation: "fence"}); err != nil {
				return retry.RetryableError(err)
			}
		}
		return retry.RetryableError(errors.New("waiting for executor container cleanup and restart"))
	})
}

func claimExecutorContainer(ctx context.Context, socket string, env map[string]string) (*executorInstance, error) {
	c, status, err := executorControl(ctx, socket, executorControlRequest{Operation: "claim", Environment: env})
	if err != nil {
		return nil, err
	}
	if !status.Owned {
		_ = c.Close()
		if status.Error != "" {
			return nil, executorRefusal{reason: status.Error}
		}
		return nil, errors.New("executor companion refused ownership")
	}
	done := make(chan error, 1)
	go func() {
		_, err := io.Copy(io.Discard, c)
		done <- errors.Join(errors.New("executor container ownership connection lost"), err)
	}()
	return &executorInstance{done: done, stop: func(ctx context.Context) error {
		_ = c.Close()
		return fenceExecutorContainer(ctx, socket, status.Generation)
	}}, nil
}
