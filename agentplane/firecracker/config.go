package firecracker

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
)

type WorkspaceKind string

const (
	WorkspaceKindRawDiskImage    WorkspaceKind = "RawDiskImage"
	WorkspaceKindBlockFilesystem WorkspaceKind = "BlockFilesystem"
)

type Config struct {
	PodUID             string        `json:"pod_uid"`
	SandboxUID         string        `json:"sandbox_uid"`
	VCPUCount          int           `json:"vcpu_count"`
	MemoryMiB          int           `json:"memory_mib"`
	BootImage          string        `json:"boot_image"`
	RunnerImage        string        `json:"runner_image"`
	BootDirectory      string        `json:"boot_directory"`
	RunnerDirectory    string        `json:"runner_directory"`
	WorkspaceKind      WorkspaceKind `json:"workspace_kind"`
	StateSizeMiB       int           `json:"state_size_mib"`
	WorkspaceSizeMiB   int           `json:"workspace_size_mib"`
	RelayPort          int           `json:"relay_port"`
	PlatformRoutePorts []int         `json:"platform_route_ports"`
}

func defaultConfig() Config {
	return Config{
		BootDirectory:      "/images/boot",
		RunnerDirectory:    "/images/runner",
		StateSizeMiB:       2048,
		WorkspaceSizeMiB:   8192,
		RelayPort:          3128,
		PlatformRoutePorts: []int{},
	}
}

// LoadConfig reads the launcher's strict JSON settings, then applies the
// Pod-owned identity and image environment overrides.
func LoadConfig(path string) (Config, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return Config{}, err
	}
	trimmed := bytes.TrimSpace(data)
	if len(trimmed) == 0 || trimmed[0] != '{' {
		return Config{}, fmt.Errorf("launcher configuration must be a JSON object")
	}
	var values map[string]json.RawMessage
	if err := json.Unmarshal(data, &values); err != nil {
		return Config{}, fmt.Errorf("decode launcher configuration: %w", err)
	}
	for name, value := range values {
		if bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
			return Config{}, fmt.Errorf("launcher configuration field %q cannot be null", name)
		}
	}
	config := defaultConfig()
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&config); err != nil {
		return Config{}, fmt.Errorf("decode launcher configuration: %w", err)
	}
	var trailing any
	if err := decoder.Decode(&trailing); err != io.EOF {
		if err == nil {
			return Config{}, fmt.Errorf("launcher configuration contains trailing JSON")
		}
		return Config{}, fmt.Errorf("decode launcher configuration trailer: %w", err)
	}
	if err := applyConfigEnvironment(&config); err != nil {
		return Config{}, err
	}
	if err := config.Validate(); err != nil {
		return Config{}, err
	}
	return config, nil
}

func applyConfigEnvironment(config *Config) error {
	for _, override := range []struct {
		name string
		set  func(string) error
	}{
		{name: "POD_UID", set: func(value string) error { config.PodUID = value; return nil }},
		{name: "SANDBOX_UID", set: func(value string) error { config.SandboxUID = value; return nil }},
		{name: "FIRECRACKER_VCPU_COUNT", set: func(value string) error {
			parsed, err := strconv.Atoi(strings.TrimSpace(value))
			if err != nil {
				return fmt.Errorf("FIRECRACKER_VCPU_COUNT must be an integer: %w", err)
			}
			config.VCPUCount = parsed
			return nil
		}},
		{name: "FIRECRACKER_MEMORY_MIB", set: func(value string) error {
			parsed, err := strconv.Atoi(strings.TrimSpace(value))
			if err != nil {
				return fmt.Errorf("FIRECRACKER_MEMORY_MIB must be an integer: %w", err)
			}
			config.MemoryMiB = parsed
			return nil
		}},
		{name: "FIRECRACKER_BOOT_IMAGE", set: func(value string) error { config.BootImage = value; return nil }},
		{name: "FIRECRACKER_RUNNER_IMAGE", set: func(value string) error { config.RunnerImage = value; return nil }},
		{name: "WORKSPACE_KIND", set: func(value string) error { config.WorkspaceKind = WorkspaceKind(value); return nil }},
	} {
		value, ok := os.LookupEnv(override.name)
		if !ok {
			continue
		}
		if err := override.set(value); err != nil {
			return err
		}
	}
	return nil
}

func (config Config) Validate() error {
	if config.PodUID == "" || config.SandboxUID == "" {
		return fmt.Errorf("Pod and Sandbox UIDs must be non-empty")
	}
	if config.VCPUCount < 1 || config.VCPUCount > 32 {
		return fmt.Errorf("VCPU count must be between 1 and 32")
	}
	if config.MemoryMiB < 128 {
		return fmt.Errorf("memory must be at least 128 MiB")
	}
	if config.BootImage == "" || config.RunnerImage == "" {
		return fmt.Errorf("boot and runner images must be non-empty")
	}
	if config.WorkspaceKind != WorkspaceKindRawDiskImage && config.WorkspaceKind != WorkspaceKindBlockFilesystem {
		return fmt.Errorf("unsupported workspace kind %q", config.WorkspaceKind)
	}
	if config.StateSizeMiB < 256 || config.WorkspaceSizeMiB < 256 {
		return fmt.Errorf("state and workspace sizes must be at least 256 MiB")
	}
	if config.RelayPort < 1 || config.RelayPort > 65535 {
		return fmt.Errorf("relay port must be between 1 and 65535")
	}
	if config.PlatformRoutePorts == nil {
		return fmt.Errorf("platform route ports must be an array")
	}
	seen := make(map[int]struct{}, len(config.PlatformRoutePorts))
	for _, port := range config.PlatformRoutePorts {
		if port < 1 || port > 65535 || port == 9090 || port == 9092 {
			return fmt.Errorf("platform route ports must be unique, valid, and distinct from control ports")
		}
		if _, ok := seen[port]; ok {
			return fmt.Errorf("platform route ports must be unique, valid, and distinct from control ports")
		}
		seen[port] = struct{}{}
	}
	return nil
}
