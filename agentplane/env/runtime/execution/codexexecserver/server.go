package codexexecserver

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
	"time"

	executorcontrol "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/companion/control"
	controlpb "github.com/agentydragon/ducktape/agentplane/env/runtime/execution/companion/control/controlpb"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
	"github.com/coder/websocket"
	"github.com/sethvargo/go-retry"
	"go.uber.org/zap"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const (
	ImplementationNative = "native"
	ImplementationCodex  = "codex"

	healthInterval     = 5 * time.Second
	healthDeadline     = 3 * time.Second
	healthFailureLimit = 3
	fenceDeadline      = 90 * time.Second
	executorStopGrace  = 5 * time.Second
)

type (
	Start func(context.Context) (*supervision.Instance, error)
	Probe func(context.Context) error
)

// Readiness contains the independently configurable executor startup windows.
type Readiness struct {
	Timeout       time.Duration
	ProbeTimeout  time.Duration
	ProbeInterval time.Duration
}

// Resolve supplies defaults for omitted windows and rejects invalid direct Go configuration.
func (r Readiness) Resolve() (Readiness, error) {
	if err := resolveDuration("execution backend readiness timeout", &r.Timeout, 20*time.Second); err != nil {
		return Readiness{}, err
	}
	if err := resolveDuration("execution backend readiness probe timeout", &r.ProbeTimeout, 2*time.Second); err != nil {
		return Readiness{}, err
	}
	if err := resolveDuration("execution backend readiness probe interval", &r.ProbeInterval, 100*time.Millisecond); err != nil {
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

// ErrExecutionBackendLost marks an established executor that stopped or became
// unhealthy. It is only a diagnostic; the caller must still prove the backend's
// process domain was fenced before treating the loss as recoverable.
var ErrExecutionBackendLost = errors.New("execution backend lost")

// ErrContainerOwnershipLost means the companion closed an established ownership
// lease. It is a more specific diagnostic for Pod backends.
var ErrContainerOwnershipLost = errors.New("executor container ownership connection lost")

const (
	PlacementDirect  = "direct"
	PlacementSystemd = "systemd"
)

type Owner string

const (
	OwnerCompanion Owner = "companion"
	OwnerSystemd   Owner = "systemd"
)

// Config describes the executor's process owner separately from command placement.
type Config struct {
	Owner            Owner
	URL              string
	Implementation   string
	Placement        string
	WorkloadLauncher *supervision.SystemdLauncher
	LimitsJSON       string
	ContainerSocket  string
	Launcher         *supervision.SystemdLauncher
	Readiness        Readiness
}

func (c Config) Validate(harnessLauncher *supervision.SystemdLauncher) error {
	if c.Placement != PlacementDirect && c.Placement != PlacementSystemd {
		return errors.New("unknown command placement")
	}
	if c.Placement == PlacementSystemd {
		if c.Implementation == ImplementationCodex || c.Owner != OwnerSystemd || c.WorkloadLauncher == nil {
			return errors.New("systemd command placement requires a native executor with a systemd owner and workload limits")
		}
	} else if c.WorkloadLauncher != nil {
		return errors.New("workload launcher requires systemd command placement")
	}
	if _, err := ResolveImplementation(c.Implementation); err != nil {
		return err
	}
	if err := ValidateURL(c.URL); err != nil {
		return err
	}
	if _, err := c.Readiness.Resolve(); err != nil {
		return err
	}
	switch c.Owner {
	case OwnerCompanion:
		if !filepath.IsAbs(c.ContainerSocket) || c.Launcher != nil || harnessLauncher != nil {
			return errors.New("companion executor requires an absolute ownership socket and separate process supervision")
		}
		return nil
	case OwnerSystemd:
		if c.ContainerSocket != "" {
			return errors.New("systemd executor cannot use a companion socket")
		}
	default:
		return errors.New("executor requires an explicit owner")
	}
	if harnessLauncher == nil || c.Launcher == nil {
		return errors.New("supervised executor requires distinct workload and executor launchers with explicit leaf limits")
	}
	if harnessLauncher.Slice != supervision.WorkloadSlice || c.Launcher.Slice != supervision.ExecutorSlice {
		return errors.New("executor and harness launchers require their distinct systemd slices")
	}
	for _, launcher := range []*supervision.SystemdLauncher{harnessLauncher, c.Launcher} {
		if err := launcher.AggregateLimits.Validate(); err != nil {
			return err
		}
		if err := launcher.ValidateProcessLimits(launcher.ProcessLimits); err != nil {
			return err
		}
	}
	return nil
}

// ResolveImplementation defaults an unspecified implementation to the native executor.
func ResolveImplementation(value string) (string, error) {
	switch value {
	case "", ImplementationNative:
		return ImplementationNative, nil
	case ImplementationCodex:
		return ImplementationCodex, nil
	default:
		return "", fmt.Errorf("unknown execution backend implementation %q", value)
	}
}

// Command builds the supervised process command for the selected execution implementation.
func Command(implementation, binary, endpoint, limitsJSON string) ([]string, error) {
	implementation, err := ResolveImplementation(implementation)
	if err != nil {
		return nil, err
	}
	if implementation == ImplementationCodex {
		if limitsJSON != "" {
			return nil, errors.New("execution limits are configurable only for the native implementation")
		}
		return []string{binary, "exec-server", "--listen", endpoint}, nil
	}
	command := []string{binary, "serve", "--listen", endpoint}
	if limitsJSON != "" {
		command = append(command, "--limits-json", limitsJSON)
	}
	return command, nil
}

type NativeConfig struct {
	Config            Config
	Binary            string
	StateDir          string
	Environment       map[string]string
	Supervisor        *supervision.Supervisor
	CommandSupervisor *supervision.Supervisor
	// LaunchProcess binds an embedded native executor to Runtime's process
	// controller.
	LaunchProcess func(context.Context, supervision.ProcessConfig) (*supervision.ManagedProcess, error)
}

type nativeProcess interface {
	PID() int
	Stdout() io.Reader
	Stderr() io.Reader
	Wait(context.Context) (int, error)
	Stop(context.Context) error
	ExitDescription() string
}

// NativeStart creates the platform-specific launch adapter used by the execution backend lifecycle.
func NativeStart(config NativeConfig) Start {
	return func(ctx context.Context) (*supervision.Instance, error) {
		env := maps.Clone(config.Environment)
		if env == nil {
			env = make(map[string]string)
		}
		for _, name := range []string{"OPENAI_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_BASE_URL", "CODEX_HOME"} {
			delete(env, name)
		}
		switch config.Config.Owner {
		case OwnerCompanion:
			return claimContainer(ctx, config.Config.ContainerSocket, env, config.Config.Implementation, config.Config.LimitsJSON)
		case OwnerSystemd:
		default:
			return nil, errors.New("execution backend requires an explicit owner")
		}
		if config.Supervisor == nil && config.LaunchProcess == nil {
			return nil, errors.New("execution backend supervisor is not configured")
		}
		command, err := Command(config.Config.Implementation, config.Binary, config.Config.URL, config.Config.LimitsJSON)
		if err != nil {
			return nil, err
		}
		var commandListener *net.UnixListener
		if config.Config.Placement == PlacementSystemd {
			if config.CommandSupervisor == nil {
				return nil, errors.New("systemd placement requires command supervisor")
			}
			if err := config.CommandSupervisor.WaitCommandScope(ctx); err != nil {
				return nil, fmt.Errorf("command scope readiness: %w", err)
			}
			commandListener, err = supervision.ListenCommandSocket("/run/agentplane-environment-runtime-lifetime/execution-commands.sock")
			if err != nil {
				return nil, err
			}
			command = append(command, "--command-socket", commandListener.Addr().String())
		}
		launched := false
		defer func() {
			if !launched && commandListener != nil {
				_ = commandListener.Close()
			}
		}()
		processConfig := supervision.ProcessConfig{
			Command:      command,
			Dir:          config.StateDir,
			Env:          env,
			StopGrace:    executorStopGrace,
			FenceTimeout: fenceDeadline,
		}
		var process nativeProcess
		if config.LaunchProcess != nil {
			managed, err := config.LaunchProcess(ctx, processConfig)
			if err != nil {
				return nil, err
			}
			process = managed
		} else {
			native, err := config.Supervisor.NewProcess(processConfig)
			if err != nil {
				return nil, err
			}
			if err := native.Start(ctx); err != nil {
				return nil, err
			}
			process = native
		}
		done := make(chan error, 1)
		var stopCommands context.CancelFunc
		var commandsDone chan struct{}
		var commandsErr error
		if commandListener != nil {
			var bridgeCtx context.Context
			bridgeCtx, stopCommands = context.WithCancel(context.Background())
			commandsDone = make(chan struct{})
			go func() {
				commandsErr = supervision.ServeCommands(bridgeCtx, commandListener, config.CommandSupervisor, config.Binary, process.PID)
				close(commandsDone)
				if bridgeCtx.Err() == nil {
					select {
					case done <- errors.Join(ErrExecutionBackendLost, errors.New("command supervision stopped"), commandsErr):
					default:
					}
				}
			}()
		}

		launched = true
		go func() { _, _ = io.Copy(io.Discard, process.Stdout()) }()
		go func() {
			buffer := make([]byte, 4096)
			for {
				n, err := process.Stderr().Read(buffer)
				if n > 0 {
					zap.L().Info("execution backend stderr", zap.String("text", string(buffer[:n])))
				}
				if err != nil {
					return
				}
			}
		}()
		go func() {
			code, err := process.Wait(context.Background())
			select {
			case done <- errors.Join(ErrExecutionBackendLost, fmt.Errorf("execution backend exited: code=%d; %s", code, process.ExitDescription()), err):
			default:
			}
		}()
		stop := func(ctx context.Context) error {
			err := process.Stop(ctx)
			if stopCommands != nil {
				stopCommands()
				_ = commandListener.Close()
				select {
				case <-commandsDone:
					err = errors.Join(err, commandsErr)
				case <-ctx.Done():
					err = errors.Join(err, ctx.Err())
				}
			}
			return err
		}
		return &supervision.Instance{PID: process.PID(), Done: done, Stop: stop}, nil
	}
}

func ProbeAt(endpoint string) Probe {
	return func(ctx context.Context) error { return probe(ctx, endpoint) }
}

// Monitor bounds backend liveness for the embedded service lifetime; label names the probed surface in the loss error.
func Monitor(ctx context.Context, label string, check Probe) error {
	ticker := time.NewTicker(healthInterval)
	defer ticker.Stop()
	failures := 0
	for {
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		}
		attempt, cancel := context.WithTimeout(ctx, healthDeadline)
		err := check(attempt)
		cancel()
		if ctx.Err() != nil {
			return nil
		}
		if err == nil {
			failures = 0
			continue
		}
		failures++
		if failures >= healthFailureLimit {
			return errors.Join(ErrExecutionBackendLost, fmt.Errorf("%s: %w", label, err))
		}
	}
}

func ValidateURL(value string) error {
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

func (r Refusal) Error() string {
	return "executor companion refused to start the backend: " + r.Reason
}

// FenceContainer waits for an idle generation at startup, or proof that the
// specified previous owner's generation was fenced during shutdown.
func FenceContainer(ctx context.Context, socket, previous string) error {
	return retry.Do(ctx, retry.NewConstant(200*time.Millisecond), func(ctx context.Context) error {
		attempt, cancel := context.WithTimeout(ctx, 2*time.Second)
		defer cancel()
		connection, err := executorcontrol.Dial(socket)
		if err != nil {
			return retry.RetryableError(err)
		}
		defer connection.Close()
		client := controlpb.NewCompanionControlClient(connection)
		current, err := client.Inspect(attempt, &controlpb.InspectRequest{})
		if err != nil {
			return retry.RetryableError(err)
		}
		if current.GetGeneration() == "" {
			return retry.RetryableError(errors.New("executor companion returned no generation"))
		}
		if previous != "" && current.GetGeneration() != previous {
			// Generation changes only after a proven fence (or whole-container replacement).
			// A successor may already have claimed the new generation.
			return nil
		}
		if !current.GetOwned() && current.GetGeneration() != previous {
			return nil
		}
		if current.GetOwned() {
			if _, err := client.Fence(attempt, &controlpb.FenceRequest{Generation: current.GetGeneration()}); err != nil {
				return retry.RetryableError(err)
			}
		}
		return retry.RetryableError(errors.New("waiting for executor process-domain fence"))
	})
}

func claimContainer(ctx context.Context, socket string, env map[string]string, implementation, limitsJSON string) (*supervision.Instance, error) {
	connection, err := executorcontrol.Dial(socket)
	if err != nil {
		return nil, err
	}
	// The handshake is bounded by ctx; the established stream lives until Stop.
	leaseCtx, cancelLease := context.WithCancel(context.Background())
	stopHandshake := context.AfterFunc(ctx, cancelLease)
	stream, err := controlpb.NewCompanionControlClient(connection).Claim(leaseCtx, &controlpb.ClaimRequest{Environment: env, Implementation: implementation, LimitsJson: limitsJSON})
	var claim *controlpb.ClaimResponse
	if err == nil {
		claim, err = stream.Recv()
	}
	stopHandshake()
	if err == nil && ctx.Err() != nil {
		err = ctx.Err()
	}
	if err != nil {
		cancelLease()
		_ = connection.Close()
		if status.Code(err) == codes.FailedPrecondition {
			return nil, Refusal{Reason: status.Convert(err).Message()}
		}
		return nil, err
	}
	if claim.GetGeneration() == "" {
		cancelLease()
		_ = connection.Close()
		return nil, errors.New("executor companion returned no generation")
	}
	done := make(chan error, 1)
	go func() {
		_, err := stream.Recv()
		done <- errors.Join(ErrExecutionBackendLost, ErrContainerOwnershipLost, err)
	}()
	return &supervision.Instance{Done: done, Stop: func(ctx context.Context) error {
		cancelLease()
		_ = connection.Close()
		return FenceContainer(ctx, socket, claim.GetGeneration())
	}}, nil
}
