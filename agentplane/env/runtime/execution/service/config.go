// Package service owns Runtime's embedded execution adapter and supervised backend.
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
	CodexBinary      string
	ContainerSocket  string
	ExecutorURL      string
	Implementation   string
	LimitsJSON       string
	Environment      map[string]string
	Backend          string
	Readiness        codexexecserver.Readiness
	// ProcessHost binds native systemd work to Runtime's process finalizers.
	ProcessHost       *supervision.Host
	Supervisor        *supervision.Supervisor
	ProcessGeneration uint64
}

// Resolve supplies the executor's compatible default readiness windows and validates the
// complete configuration before it is handed to the service runtime.
func (c Config) Resolve() (Config, error) {
	readiness, err := c.Readiness.Resolve()
	if err != nil {
		return Config{}, err
	}
	c.Readiness = readiness
	c.Implementation, err = codexexecserver.ResolveImplementation(c.Implementation)
	if err != nil {
		return Config{}, err
	}
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
	if c.Implementation == codexexecserver.ImplementationCodex && c.LimitsJSON != "" {
		return errors.New("execution limits are configurable only for the native implementation")
	}
	if err := codexexecserver.ValidateURL(c.ExecutorURL); err != nil {
		return err
	}
	if c.Backend != "companion" {
		if !filepath.IsAbs(c.binary()) || !filepath.IsAbs(c.WorkingDirectory) || c.ContainerSocket != "" {
			return errors.New("direct execution requires absolute binary and working directory and no companion socket")
		}
	}
	switch c.Backend {
	case "companion":
		if !filepath.IsAbs(c.ContainerSocket) || c.Supervisor != nil || c.Binary != "" || c.CodexBinary != "" || c.WorkingDirectory != "" {
			return errors.New("companion execution requires an absolute ownership socket; its process configuration belongs to the companion")
		}
	case "systemd":
		if c.Supervisor == nil || c.ProcessHost == nil {
			return errors.New("systemd execution requires Runtime's reconciled scope and process host")
		}
	default:
		return fmt.Errorf("unknown execution backend %q", c.Backend)
	}
	return nil
}

func (c Config) binary() string {
	if c.Implementation == codexexecserver.ImplementationCodex {
		return c.CodexBinary
	}
	return c.Binary
}
