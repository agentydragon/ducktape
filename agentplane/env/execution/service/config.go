// Package service owns one environment's standalone execution process and gRPC listener.
package service

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/agentydragon/ducktape/agentplane/env/execution/codexexecserver"
	"github.com/agentydragon/ducktape/agentplane/env/supervision"
)

// Config is immutable for a service lifetime. The environment owner supplies it;
// a different incarnation requires stopping and fencing this service first.
type Config struct {
	Namespace        string            `json:"namespace"`
	SandboxName      string            `json:"sandbox_name"`
	SandboxUID       string            `json:"sandbox_uid"`
	IncarnationUID   string            `json:"incarnation_uid"`
	WorkingDirectory string            `json:"working_directory"`
	StateDir         string            `json:"state_dir"`
	Binary           string            `json:"binary"`
	ContainerSocket  string            `json:"container_socket,omitempty"`
	ExecutorURL      string            `json:"executor_url"`
	Environment      map[string]string `json:"environment"`
	Backend          string            `json:"backend"`
	Systemd          *SystemdConfig    `json:"systemd,omitempty"`
}

type SystemdConfig struct {
	AgentUID        uint32                     `json:"agent_uid"`
	AgentGID        uint32                     `json:"agent_gid"`
	BlockDevices    []string                   `json:"block_devices"`
	AggregateLimits supervision.ResourceLimits `json:"aggregate_limits"`
	ProcessLimits   supervision.ResourceLimits `json:"process_limits"`
}

func Load(path string) (Config, error) {
	file, err := os.Open(path)
	if err != nil {
		return Config{}, err
	}
	defer file.Close()
	decoder := json.NewDecoder(file)
	decoder.DisallowUnknownFields()
	var config Config
	if err := decoder.Decode(&config); err != nil {
		return Config{}, err
	}
	if err := decoder.Decode(new(any)); err != io.EOF {
		return Config{}, errors.New("execution configuration must contain exactly one JSON object")
	}
	return config, config.Validate()
}

func (c Config) Validate() error {
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
	case "pod":
		if c.Systemd != nil {
			return errors.New("Pod execution cannot configure systemd")
		}
	case "companion":
		if !filepath.IsAbs(c.ContainerSocket) || c.Systemd != nil || c.Binary != "" || c.WorkingDirectory != "" {
			return errors.New("companion execution requires an absolute ownership socket; its process configuration belongs to the companion")
		}
	case "systemd":
		if c.Systemd == nil {
			return errors.New("systemd execution requires explicit resource limits")
		}
	default:
		return fmt.Errorf("unknown execution backend %q", c.Backend)
	}
	return nil
}

func (c Config) Socket() string { return filepath.Join(c.StateDir, "execution.sock") }
