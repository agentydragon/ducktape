package runner

import (
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"net"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/agentydragon/ducktape/agentplane/runner/codexexecserver"
	"github.com/agentydragon/ducktape/agentplane/runner/harness"
	"github.com/agentydragon/ducktape/agentplane/runner/session"
	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"go.uber.org/zap"
)

// Configuration is the file contract used by every Runner deployment. Sandbox
// identity/bootstrap are supplied separately by the Sandbox Controller.
type Configuration struct {
	Version                     int                           `json:"version"`
	SessionReconcileConcurrency *int                          `json:"session_reconcile_concurrency"`
	StateDir                    string                        `json:"state_dir"`
	WorkspaceRoot               *string                       `json:"workspace_root"`
	InitializationCwd           string                        `json:"initialization_cwd"`
	Control                     RunnerControlConfiguration    `json:"control"`
	Ingress                     Endpoint                      `json:"ingress"`
	PlatformInstructions        string                        `json:"platform_instructions"`
	RunnerEnvironment           EnvironmentVariables          `json:"runner_environment"`
	HarnessEnvironment          *EnvironmentVariables         `json:"harness_environment"`
	Claude                      *HarnessConfiguration         `json:"claude"`
	Codex                       *HarnessConfiguration         `json:"codex"`
	Environment                 json.RawMessage               `json:"environment"`
	DebugCheckpoint             *DebugCheckpointConfiguration `json:"debug_checkpoint"`
}

const defaultSessionReconcileConcurrency = 16

type DebugCheckpointConfiguration struct {
	Name      string `json:"name"`
	CommandID string `json:"command_id"`
}

type EnvironmentVariables struct {
	Inherit []string          `json:"inherit"`
	Values  map[string]string `json:"values"`
}

func (e EnvironmentVariables) resolve(environ map[string]string) (map[string]string, error) {
	child := map[string]string{}
	for _, name := range e.Inherit {
		if err := validateEnvironmentName(name); err != nil {
			return nil, err
		}
		if value, ok := environ[name]; ok {
			child[name] = value
		} else {
			zap.L().Warn("inherited variable is absent from Runner environment", zap.String("name", name))
		}
	}
	for name, value := range e.Values {
		if err := validateEnvironmentName(name); err != nil {
			return nil, err
		}
		if strings.ContainsRune(value, 0) {
			return nil, fmt.Errorf("environment value %s contains NUL", name)
		}
		child[name] = value
	}
	return child, nil
}

func validateEnvironmentName(name string) error {
	if name == "" || strings.ContainsAny(name, "=\x00") {
		return errors.New("environment names must be nonempty and contain neither '=' nor NUL")
	}
	return nil
}

type HarnessConfiguration struct {
	Binary   string `json:"binary"`
	BaseURL  string `json:"base_url"`
	TokenEnv string `json:"token_env"`
}

func (h *HarnessConfiguration) resolve(environ map[string]string) (*harness.LaunchConfig, error) {
	if h == nil {
		return nil, nil
	}
	if !filepath.IsAbs(h.Binary) {
		return nil, errors.New("harness executable must be absolute")
	}
	u, err := url.Parse(h.BaseURL)
	if err != nil {
		return nil, err
	}
	if (u.Scheme != "http" && u.Scheme != "https") || u.Hostname() == "" || u.User != nil || u.RawQuery != "" || u.ForceQuery || u.Fragment != "" || u.Opaque != "" {
		return nil, errors.New("provider URL must be absolute HTTP(S) without credentials, query or fragment")
	}
	if u.Port() != "" {
		port, err := strconv.Atoi(u.Port())
		if err != nil || port < 1 || port > 65535 {
			return nil, errors.New("invalid provider URL port")
		}
	}
	if err := validateEnvironmentName(h.TokenEnv); err != nil {
		return nil, err
	}
	token := environ[h.TokenEnv]
	if token == "" {
		return nil, fmt.Errorf("provider token environment %s is required", h.TokenEnv)
	}
	return &harness.LaunchConfig{Binary: h.Binary, BaseURL: strings.TrimRight(h.BaseURL, "/"), Token: token}, nil
}

type ContainerExecutorConfiguration struct {
	URL    string `json:"url"`
	Socket string `json:"socket"`
}

type PodEnvironmentConfiguration struct {
	Kind        string                          `json:"kind"`
	Supervision string                          `json:"supervision"`
	Executor    *ContainerExecutorConfiguration `json:"executor"`
}

type VMExecutorConfiguration struct {
	URL string `json:"url"`
}

type VMEnvironmentConfiguration struct {
	Kind             string                   `json:"kind"`
	Resources        guestResources           `json:"resources"`
	ConsoleAutologin bool                     `json:"console_autologin"`
	AgentUID         uint32                   `json:"agent_uid"`
	AgentGID         uint32                   `json:"agent_gid"`
	BlockDevices     []string                 `json:"block_devices"`
	Executor         *VMExecutorConfiguration `json:"executor"`
}

func (v VMEnvironmentConfiguration) policy(slice string, aggregate, process supervision.ResourceLimits) *supervision.SystemdPolicy {
	return &supervision.SystemdPolicy{Slice: slice, AgentUID: v.AgentUID, AgentGID: v.AgentGID, BlockDevices: v.BlockDevices, AggregateLimits: aggregate, ProcessLimits: process}
}

// The environment selects resource ownership and executor transport together.
func (f Configuration) resolveEnvironment() (*supervision.SystemdPolicy, *codexexecserver.Config, *VMEnvironmentConfiguration, bool, error) {
	var tag struct {
		Kind string `json:"kind"`
	}
	if err := json.Unmarshal(f.Environment, &tag); err != nil {
		return nil, nil, nil, false, fmt.Errorf("environment is required: %w", err)
	}
	var isolation *supervision.SystemdPolicy
	var executor *codexexecserver.Config
	var vm *VMEnvironmentConfiguration
	var podPID1 bool
	switch tag.Kind {
	case "pod":
		var pod PodEnvironmentConfiguration
		if err := DecodeStrict(f.Environment, &pod); err != nil {
			return nil, nil, nil, false, err
		}
		if pod.Supervision != "pid1" && pod.Supervision != "subprocess" {
			return nil, nil, nil, false, errors.New("Pod supervision must explicitly select pid1 or subprocess")
		}
		podPID1 = pod.Supervision == "pid1"
		if pod.Executor != nil {
			executor = &codexexecserver.Config{URL: pod.Executor.URL, ContainerSocket: pod.Executor.Socket}
			if !filepath.IsAbs(pod.Executor.Socket) {
				return nil, nil, nil, false, errors.New("container executor socket must be absolute")
			}
		}
	case "vm":
		vm = &VMEnvironmentConfiguration{}
		if err := DecodeStrict(f.Environment, vm); err != nil {
			return nil, nil, nil, false, err
		}
		if err := vm.Resources.Validate(); err != nil {
			return nil, nil, nil, false, err
		}
		if vm.AgentUID == 0 || vm.AgentGID == 0 || len(vm.BlockDevices) == 0 {
			return nil, nil, nil, false, errors.New("VM supervision requires non-root UID/GID and block devices")
		}
		for _, path := range vm.BlockDevices {
			if !filepath.IsAbs(path) {
				return nil, nil, nil, false, errors.New("block device path must be absolute")
			}
		}
		isolation = vm.policy(supervision.WorkloadSlice, vm.Resources.Aggregate, vm.Resources.Process)
		if (vm.Executor == nil) != (vm.Resources.Executor == nil) {
			return nil, nil, nil, false, errors.New("executor endpoint and resource budget must be supplied together")
		}
		if vm.Executor != nil {
			executor = &codexexecserver.Config{URL: vm.Executor.URL, Isolation: vm.policy(supervision.ExecutorSlice, *vm.Resources.Executor, *vm.Resources.Executor)}
		}
	default:
		return nil, nil, nil, false, fmt.Errorf("unknown environment kind %q", tag.Kind)
	}
	if executor != nil {
		if f.Codex == nil {
			return nil, nil, nil, false, errors.New("executor requires Codex")
		}
		if err := executor.Validate(isolation); err != nil {
			return nil, nil, nil, false, err
		}
	}
	return isolation, executor, vm, podPID1, nil
}

func readConfiguration(path string) (Configuration, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Configuration{}, err
	}
	var file Configuration
	if err := DecodeStrict(data, &file); err != nil {
		return Configuration{}, fmt.Errorf("invalid Runner configuration: %w", err)
	}
	if file.Version != 2 {
		return Configuration{}, errors.New("Runner configuration version must be 2")
	}
	return file, nil
}

// LoadConfiguration validates and composes template policy, the image's explicitly
// inherited environment, and controller-owned launch data before opening state.
func LoadConfiguration(path, launchPath string) (Config, Endpoint, error) {
	config, ingress, environment, err := loadConfiguration(path, launchPath)
	if err != nil {
		return Config{}, Endpoint{}, err
	}
	if err := validateControlIdentity(config); err != nil {
		return Config{}, Endpoint{}, err
	}
	for name, value := range environment {
		if err := os.Setenv(name, value); err != nil {
			return Config{}, Endpoint{}, err
		}
	}
	return config, ingress, nil
}

func loadConfiguration(path, launchPath string) (Config, Endpoint, map[string]string, error) {
	file, err := readConfiguration(path)
	if err != nil {
		return Config{}, Endpoint{}, nil, err
	}
	config, environment, err := file.compose(launchPath)
	return config, file.Ingress, environment, err
}

func validateRunnerControl(control RunnerControlConfiguration) error {
	if control.TLS.CertFile == "" || control.TLS.KeyFile == "" || control.TLS.CAFile == "" ||
		!filepath.IsAbs(control.TLS.CertFile) || !filepath.IsAbs(control.TLS.KeyFile) || !filepath.IsAbs(control.TLS.CAFile) {
		return errors.New("control TLS cert_file, key_file, and ca_file must be absolute paths")
	}
	address := control.Target
	field := "control.target"
	switch control.Mode {
	case "reverse":
		if control.Target == "" || control.ListenAddress != "" {
			return errors.New("reverse control requires target and forbids listen_address")
		}
	case "forward":
		address = control.ListenAddress
		field = "control.listen_address"
		if control.ListenAddress == "" || control.Target != "" {
			return errors.New("forward control requires listen_address and forbids target")
		}
	default:
		return errors.New("control.mode must explicitly select forward or reverse")
	}
	host, port, err := net.SplitHostPort(address)
	number, numberErr := strconv.Atoi(port)
	if err != nil || host == "" || numberErr != nil || number < 1 || number > 65535 {
		return fmt.Errorf("%s must be a valid host:port", field)
	}
	return nil
}

func (f Configuration) compose(launchPath string) (Config, map[string]string, error) {
	sessionReconcileConcurrency := defaultSessionReconcileConcurrency
	if f.SessionReconcileConcurrency != nil {
		if *f.SessionReconcileConcurrency <= 0 {
			return Config{}, nil, errors.New("session_reconcile_concurrency must be positive")
		}
		sessionReconcileConcurrency = *f.SessionReconcileConcurrency
	}
	if !filepath.IsAbs(f.StateDir) || !filepath.IsAbs(f.InitializationCwd) || (f.WorkspaceRoot != nil && !filepath.IsAbs(*f.WorkspaceRoot)) {
		return Config{}, nil, errors.New("state, initialization and configured workspace paths must be absolute")
	}
	if err := validateRunnerControl(f.Control); err != nil {
		return Config{}, nil, err
	}
	if f.Ingress.Target == "" || f.Ingress.Bearer == "" {
		return Config{}, nil, errors.New("invalid ingress endpoint")
	}
	for name, endpoint := range map[string]Endpoint{"ingress": f.Ingress} {
		host, port, err := net.SplitHostPort(endpoint.Target)
		number, numberErr := strconv.Atoi(port)
		if err != nil || host == "" || numberErr != nil || number < 1 || number > 65535 {
			return Config{}, nil, fmt.Errorf("invalid %s endpoint", name)
		}
	}
	if strings.TrimSpace(f.PlatformInstructions) == "" {
		return Config{}, nil, errors.New("platform instructions must be nonempty")
	}
	environ := map[string]string{}
	for _, entry := range os.Environ() {
		key, value, _ := strings.Cut(entry, "=")
		environ[key] = value
	}
	environment, err := f.RunnerEnvironment.resolve(environ)
	if err != nil {
		return Config{}, nil, err
	}
	maps.Copy(environ, environment)
	if f.HarnessEnvironment == nil {
		return Config{}, nil, errors.New("harness_environment is required")
	}
	child, err := f.HarnessEnvironment.resolve(environ)
	if err != nil {
		return Config{}, nil, err
	}
	if _, ok := child["CODEX_EXEC_SERVER_URL"]; ok {
		return Config{}, nil, errors.New("CODEX_EXEC_SERVER_URL is owned by Runner executor configuration")
	}
	claude, err := f.Claude.resolve(environ)
	if err != nil {
		return Config{}, nil, err
	}
	codex, err := f.Codex.resolve(environ)
	if err != nil {
		return Config{}, nil, err
	}
	isolation, executor, _, podPID1, err := f.resolveEnvironment()
	if err != nil {
		return Config{}, nil, err
	}
	launchBytes, err := os.ReadFile(launchPath)
	if err != nil {
		return Config{}, nil, err
	}
	var launch SandboxLaunch
	if err := DecodeStrict(launchBytes, &launch); err != nil {
		return Config{}, nil, err
	}
	if launch.Sandbox.Namespace == "" || launch.Sandbox.Name == "" || launch.Sandbox.UID == "" || (launch.Bootstrap != nil && *launch.Bootstrap == "") {
		return Config{}, nil, errors.New("Sandbox namespace, name, UID and nonempty optional bootstrap are required")
	}
	launch.PlatformInstructions = strings.TrimSpace(f.PlatformInstructions)
	config := Config{
		Control:                     f.Control,
		StateDir:                    f.StateDir,
		SessionReconcileConcurrency: sessionReconcileConcurrency,
		InitializationCwd:           f.InitializationCwd,
		Launch:                      launch,
		HarnessEnvironment:          child,
		Claude:                      claude,
		Codex:                       codex,
		VMProcessPolicy:             isolation,
		PodPID1:                     podPID1,
		CodexExecServer:             executor,
	}
	if f.WorkspaceRoot != nil {
		config.WorkspaceRoot = *f.WorkspaceRoot
	}
	if f.DebugCheckpoint != nil {
		if f.DebugCheckpoint.Name == "" || f.DebugCheckpoint.CommandID == "" {
			return Config{}, nil, errors.New("checkpoint name and command ID must be nonempty")
		}
		config.DebugCheckpoint = &session.DebugCheckpointConfig{Name: f.DebugCheckpoint.Name, CommandID: f.DebugCheckpoint.CommandID}
	}
	return config, environment, nil
}
