package supervision

import (
	"context"
	"errors"
	"fmt"
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
}

// A backend owns all platform-specific process state. Process supplies the
// shared streams, PID, and completion lifecycle to it.
type processBackend interface {
	validate() error
	start(context.Context, *Process) error
	stop(context.Context, *Process) error
	description(*Process) string
}

type backendFactory func(ProcessConfig) processBackend

// Supervisor is an immutable factory for native processes.
type Supervisor struct {
	makeBackend    backendFactory
	fenceNamespace func(context.Context) error
}

// NewSubprocessSupervisor creates a supervisor that owns native process groups
// and passes stateOwnerFD through its supervisor process to each native child.
func NewSubprocessSupervisor(stateOwnerFD *os.File) *Supervisor {
	return newSubprocessSupervisor(stateOwnerFD, "")
}

func newSubprocessSupervisor(stateOwnerFD *os.File, executable string) *Supervisor {
	return &Supervisor{makeBackend: func(config ProcessConfig) processBackend {
		return &subprocessBackend{
			config:               config,
			stateOwnerFD:         stateOwnerFD,
			supervisorExecutable: executable,
		}
	}}
}

// NewProcess validates config before creating process resources.
func (s *Supervisor) NewProcess(config ProcessConfig) (*Process, error) {
	if len(config.Command) == 0 || config.Command[0] == "" {
		return nil, errCommandRequired
	}
	if config.StopGrace < 0 {
		return nil, fmt.Errorf("native process stop grace must not be negative: %s", config.StopGrace)
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
