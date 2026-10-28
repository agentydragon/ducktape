package supervision

import (
	"context"
	"os"
	"time"
)

// ProcessConfig describes one native launch. Environment values are passed
// explicitly to the process.
type ProcessConfig struct {
	Command []string
	Dir     string
	Env     map[string]string
}

// A backend owns all platform-specific process state. Process supplies the
// shared streams, PID, and completion lifecycle to it.
type processBackend interface {
	validate() error
	start(context.Context, *Process) error
	stop(context.Context, *Process, time.Duration) error
	description(*Process) string
}

type backendFactory func(ProcessConfig) processBackend

// Supervisor is an immutable factory for native processes.
type Supervisor struct {
	makeBackend backendFactory
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
	if s == nil || s.makeBackend == nil {
		return nil, errSupervisorUnconfigured
	}
	backend := s.makeBackend(config)
	if err := backend.validate(); err != nil {
		return nil, err
	}
	return &Process{backend: backend, waitDone: make(chan struct{})}, nil
}
