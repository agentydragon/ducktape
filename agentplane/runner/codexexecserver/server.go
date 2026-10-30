package codexexecserver

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"maps"
	"net/http"
	"net/url"
	"path/filepath"
	"strconv"
	"time"

	executorcontrol "github.com/agentydragon/ducktape/agentplane/runner/executor/control"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/coder/websocket"
	"github.com/sethvargo/go-retry"
	"go.uber.org/zap"
)

const (
	healthInterval    = 5 * time.Second
	healthDeadline    = 3 * time.Second
	fenceDeadline     = 90 * time.Second
	executorStopGrace = 5 * time.Second
)

// Config selects local systemd supervision or ownership of the Pod executor container.
type Config struct {
	URL             string
	ContainerSocket string
	Isolation       *supervision.SystemdPolicy
	Readiness       Readiness
}

func (c Config) Validate(harnessIsolation *supervision.SystemdPolicy) error {
	if err := validateURL(c.URL); err != nil {
		return err
	}
	if _, err := c.Readiness.Resolve(); err != nil {
		return err
	}
	if c.ContainerSocket != "" {
		if !filepath.IsAbs(c.ContainerSocket) || c.Isolation != nil || harnessIsolation != nil {
			return errors.New("container executor requires an absolute control socket and container-owned limits")
		}
		return nil
	}
	if harnessIsolation == nil || c.Isolation == nil {
		return errors.New("supervised executor requires cgroup isolation and explicit leaf limits")
	}
	if harnessIsolation.Slice != supervision.WorkloadSlice || c.Isolation.Slice != supervision.ExecutorSlice {
		return errors.New("executor and harness policies require their distinct systemd slices")
	}
	for _, groups := range []*supervision.SystemdPolicy{harnessIsolation, c.Isolation} {
		if err := groups.AggregateLimits.Validate(); err != nil {
			return err
		}
		if err := groups.ValidateProcessLimits(groups.ProcessLimits); err != nil {
			return err
		}
	}
	return nil
}

type NativeConfig struct {
	Config      Config
	Binary      string
	StateDir    string
	Environment map[string]string
	Supervisor  *supervision.Supervisor
}

// NativeStart creates the platform-specific launch adapter used by the exec-server lifecycle.
func NativeStart(config NativeConfig) Start {
	return func(ctx context.Context) (*supervision.Instance, error) {
		env := maps.Clone(config.Environment)
		if env == nil {
			env = make(map[string]string)
		}
		for _, name := range []string{"OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_BASE_URL", "CODEX_HOME"} {
			delete(env, name)
		}
		if config.Config.ContainerSocket != "" {
			return claimContainer(ctx, config.Config.ContainerSocket, env)
		}
		if config.Supervisor == nil {
			return nil, errors.New("Codex exec-server supervisor is not configured")
		}
		process, err := config.Supervisor.NewProcess(supervision.ProcessConfig{
			Command: []string{config.Binary, "exec-server", "--listen", config.Config.URL},
			Dir:     config.StateDir,
			Env:     env,
		})
		if err != nil {
			return nil, err
		}
		if err := process.Start(ctx); err != nil {
			return nil, err
		}
		go func() { _, _ = io.Copy(io.Discard, process.Stdout()) }()
		go func() {
			buffer := make([]byte, 4096)
			for {
				n, err := process.Stderr().Read(buffer)
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
			code, err := process.Wait(context.Background())
			done <- errors.Join(fmt.Errorf("Codex exec-server exited: code=%d; %s", code, process.ExitDescription()), err)
		}()
		return &supervision.Instance{
			PID: process.PID(), Done: done,
			Stop: func(ctx context.Context) error { return process.Stop(ctx, executorStopGrace) },
		}, nil
	}
}

func ProbeAt(endpoint string) Probe {
	return func(ctx context.Context) error { return probe(ctx, endpoint) }
}

func validateURL(value string) error {
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

// probe validates the executor dispatcher with a deadline on both handshake and response.
func probe(ctx context.Context, endpoint string) error {
	connection, _, err := websocket.Dial(ctx, endpoint, &websocket.DialOptions{HTTPClient: &http.Client{Transport: &http.Transport{Proxy: nil}}})
	if err != nil {
		return fmt.Errorf("executor WebSocket dial: %w", err)
	}
	defer connection.CloseNow()
	connection.SetReadLimit(65536)
	if err := connection.Write(ctx, websocket.MessageText, []byte(`{"id":1,"method":"initialize","params":{"clientName":"agentplane-health"}}`)); err != nil {
		return fmt.Errorf("executor initialize write: %w", err)
	}
	_, data, err := connection.Read(ctx)
	if err != nil {
		return fmt.Errorf("executor initialize read: %w", err)
	}
	var response struct {
		ID     int `json:"id"`
		Result struct {
			SessionID string `json:"sessionId"`
		} `json:"result"`
	}
	if err := json.Unmarshal(data, &response); err != nil {
		return fmt.Errorf("executor initialize decode: %w", err)
	}
	if response.ID != 1 || response.Result.SessionID == "" {
		return errors.New("executor health initialize returned no session")
	}
	if err := connection.Write(ctx, websocket.MessageText, []byte(`{"method":"initialized","params":null}`)); err != nil {
		return fmt.Errorf("executor initialized write: %w", err)
	}
	if err := connection.Write(ctx, websocket.MessageText, []byte(`{"id":2,"method":"environment/status","params":null}`)); err != nil {
		return fmt.Errorf("executor environment/status write: %w", err)
	}
	_, data, err = connection.Read(ctx)
	if err != nil {
		return fmt.Errorf("executor environment/status read: %w", err)
	}
	var status struct {
		ID     int `json:"id"`
		Result struct {
			Status string `json:"status"`
		} `json:"result"`
	}
	if err := json.Unmarshal(data, &status); err != nil {
		return fmt.Errorf("executor environment/status decode: %w", err)
	}
	if status.ID != 2 || status.Result.Status != "ready" {
		return fmt.Errorf("executor environment/status returned id=%d status=%q, expected id=2 status=ready", status.ID, status.Result.Status)
	}
	return nil
}

type Refusal struct{ Reason string }

func (r Refusal) Error() string { return "executor companion refused to start Codex: " + r.Reason }

// FenceContainer waits until a fresh idle container proves the old PID namespace is gone.
func FenceContainer(ctx context.Context, socket, previous string) error {
	return retry.Do(ctx, retry.NewConstant(200*time.Millisecond), func(ctx context.Context) error {
		attempt, cancel := context.WithTimeout(ctx, 2*time.Second)
		defer cancel()
		connection, status, err := executorcontrol.Connect(attempt, socket, executorcontrol.Request{Operation: "inspect"})
		if err != nil {
			return retry.RetryableError(err)
		}
		defer connection.Close()
		if !status.Owned && status.Generation != previous {
			return nil
		}
		if status.Owned {
			if err := json.NewEncoder(connection).Encode(executorcontrol.Request{Operation: "fence"}); err != nil {
				return retry.RetryableError(err)
			}
		}
		return retry.RetryableError(errors.New("waiting for executor container cleanup and restart"))
	})
}

func claimContainer(ctx context.Context, socket string, env map[string]string) (*supervision.Instance, error) {
	connection, status, err := executorcontrol.Connect(ctx, socket, executorcontrol.Request{Operation: "claim", Environment: env})
	if err != nil {
		return nil, err
	}
	if !status.Owned {
		_ = connection.Close()
		if status.Error != "" {
			return nil, Refusal{Reason: status.Error}
		}
		return nil, errors.New("executor companion refused ownership")
	}
	done := make(chan error, 1)
	go func() {
		_, err := io.Copy(io.Discard, connection)
		done <- errors.Join(errors.New("executor container ownership connection lost"), err)
	}()
	return &supervision.Instance{Done: done, Stop: func(ctx context.Context) error {
		_ = connection.Close()
		return FenceContainer(ctx, socket, status.Generation)
	}}, nil
}
