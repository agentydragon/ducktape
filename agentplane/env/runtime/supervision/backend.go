package supervision

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"time"
)

// ProcessConfig describes one native launch. Environment values are passed
// explicitly to the process, and StopGrace is fixed for that process lifetime.
type ProcessConfig struct {
	Command []string
	Dir     string
	Env     map[string]string
	// StopGrace is how long graceful shutdown can run before the backend forces
	// termination. A zero grace requests immediate escalation.
	StopGrace time.Duration
	// FenceTimeout bounds a managed process's cleanup worker. Zero keeps the
	// default allowance after StopGrace; it does not change systemd escalation.
	FenceTimeout time.Duration
}

// processHandle owns one started execution domain. Done closes after backend
// cleanup and fencing; Wait reports the backend's exit result.
type processHandle interface {
	PID() int
	Done() <-chan struct{}
	Stdin() *os.File
	Stdout() io.Reader
	Stderr() io.Reader
	CloseStdin() error
	Stop(context.Context) error
	Wait(context.Context) (int, error)
}

type processStarter interface {
	WaitStarted(context.Context) error
}

// A backend owns all platform-specific process state and returns its handle.
type processBackend interface {
	validate() error
	start(context.Context) (processHandle, error)
}

type backendFactory func(ProcessConfig) processBackend

// Supervisor is an immutable factory for native processes.
type Supervisor struct {
	commands       *SystemdLauncher
	systemdScope   SystemdScopeID
	makeBackend    backendFactory
	fenceNamespace func(context.Context) error
}

// NewProcess validates config before creating process resources.
func (s *Supervisor) NewProcess(config ProcessConfig) (*Process, error) {
	if len(config.Command) == 0 || config.Command[0] == "" {
		return nil, errCommandRequired
	}
	if config.StopGrace < 0 {
		return nil, fmt.Errorf("native process stop grace must not be negative: %s", config.StopGrace)
	}
	if config.FenceTimeout < 0 {
		return nil, fmt.Errorf("native process fence timeout must not be negative: %s", config.FenceTimeout)
	}
	if s == nil || s.makeBackend == nil {
		return nil, errSupervisorUnconfigured
	}
	backend := s.makeBackend(config)
	if err := backend.validate(); err != nil {
		return nil, err
	}
	return &Process{backend: backend, waitDone: make(chan struct{})}, nil
}

// FenceNamespace kills and waits for every process in a private PID namespace
// except PID 1. The caller must stop its managed processes first and prevent
// concurrent launches until the fence completes.
func (s *Supervisor) FenceNamespace(ctx context.Context) error {
	if s == nil || s.fenceNamespace == nil {
		return errors.New("supervisor does not own a private PID namespace")
	}
	return s.fenceNamespace(ctx)
}
