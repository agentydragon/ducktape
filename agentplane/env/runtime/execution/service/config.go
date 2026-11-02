// Package service owns Runtime's embedded execution adapter and native backend.
package service

import (
	"errors"
	"fmt"
	"path/filepath"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/codexexecserver"
	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
)

// Config is immutable for a service lifetime. The environment owner supplies it;
// a different incarnation requires stopping and fencing this service first.
type Config struct {
	Namespace        string
	SandboxName      string
	SandboxUID       string
	IncarnationUID   string
	WorkingDirectory string
	StateDir         string
	Binary           string
	ContainerSocket  string
	ExecutorURL      string
	Environment      map[string]string
	Backend          string
	Readiness        codexexecserver.Readiness
	Systemd          *SystemdConfig
	// ProcessHost binds native systemd work to Runtime's process finalizers.
	ProcessHost       *supervision.Host
	ProcessGeneration uint64
}

type SystemdConfig struct {
	AgentUID          uint32
	AgentGID          uint32
	BlockDevices      []string
	AggregateLimits   supervision.ResourceLimits
	ProcessLimits     supervision.ResourceLimits
	OwnerUnit         string
	LifetimeDirectory string
}

// Resolve supplies the executor's compatible default readiness windows and validates the
// complete configuration before it is handed to the service runtime.
func (c Config) Resolve() (Config, error) {
	readiness, err := c.Readiness.Resolve()
	if err != nil {
		return Config{}, err
	}
	c.Readiness = readiness
	if err := c.validate(); err != nil {
		return Config{}, err
	}
	return c, nil
}

func (c Config) Validate() error {
	_, err := c.Resolve()
	return err
}

func (c Config) validate() error {
	if c.Namespace == "" || c.SandboxName == "" || c.SandboxUID == "" || c.IncarnationUID == "" {
		return errors.New("execution configuration requires Sandbox namespace, name, UID and incarnation UID")
	}
	if !filepath.IsAbs(c.StateDir) {
		return errors.New("execution state directory must be absolute")
	}
	if err := codexexecserver.ValidateURL(c.ExecutorURL); err != nil {
		return err
	}
	if c.Backend != "companion" {
		if !filepath.IsAbs(c.Binary) || !filepath.IsAbs(c.WorkingDirectory) || c.ContainerSocket != "" {
			return errors.New("direct execution requires absolute binary and working directory and no companion socket")
		}
	}
	switch c.Backend {
	case "companion":
		if !filepath.IsAbs(c.ContainerSocket) || c.Systemd != nil || c.Binary != "" || c.WorkingDirectory != "" {
			return errors.New("companion execution requires an absolute ownership socket; its process configuration belongs to the companion")
		}
	case "systemd":
		if c.Systemd == nil {
			return errors.New("systemd execution requires explicit resource limits")
		}
		if c.Systemd.OwnerUnit == "" || c.Systemd.LifetimeDirectory == "" {
			return errors.New("systemd execution requires an explicit owner unit and lifetime directory")
		}
		if !filepath.IsAbs(c.Systemd.LifetimeDirectory) {
			return errors.New("systemd execution owner lifetime directory must be absolute")
		}
	default:
		return fmt.Errorf("unknown execution backend %q", c.Backend)
	}
	return nil
}
