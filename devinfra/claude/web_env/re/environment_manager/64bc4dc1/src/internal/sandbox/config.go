// Reconstructed from binary: environment-manager (Build ID 64bc4dc1)
// Source: internal/sandbox/config.go
// Original path: /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/sandbox/config.go

package sandbox

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
)

// SandboxConfig represents the full sandbox configuration.
// Type size: 136 bytes.
// JSON-serialized and written to a temp config file for the sandbox runtime.
type SandboxConfig struct {
	AllowedDomains            []string       `json:"allowedDomains"`
	DeniedDomains             []string       `json:"deniedDomains"`
	AllowWrite                []string       `json:"allowWrite"`
	DenyRead                  []string       `json:"denyRead"`
	DenyWrite                 []string       `json:"denyWrite"`
	Network                   *NetworkConfig `json:"network"`
	AllowedTools              []string       `json:"allowed_tools,omitempty"`
	DisallowedTools           []string       `json:"disallowed_tools,omitempty"`
	EnableWeakerNestedSandbox bool           `json:"enableWeakerNestedSandbox"`
	UseSandboxGatewayConfig   bool           `json:"use_sandbox_gateway_config,omitempty"`
	AllowUnrestrictedGitPush  bool           `json:"allow_unrestricted_git_push,omitempty"`
	AllowGitConfig            bool           `json:"allowGitConfig,omitempty"`
}

// NetworkConfig holds network-related sandbox configuration.
type NetworkConfig struct {
	AllowedDomains []string `json:"allowedDomains"`
	DeniedDomains  []string `json:"deniedDomains"`
}

// DefaultAllowedDomains are domains that must be present in any sandbox configuration.
var DefaultAllowedDomains = []string{
	"api.anthropic.com",
	"*.anthropic.com",
}

// DefaultDenyReadPaths are paths that should be denied from reading by default.
var DefaultDenyReadPaths = []string{
	"~/.ssh",
	"~/.aws",
	"~/.config/gcloud",
	"/etc/shadow",
	"/etc/passwd-",
	"/secrets",
}

// DefaultAllowWritePaths are paths that should be allowed for writing by default.
var DefaultAllowWritePaths = []string{}

// WriteConfigFile serializes the given SandboxConfig to JSON and writes it to
// a temporary file. Returns the temp file path and any error.
//
// Binary address: 0x7da8e0
func WriteConfigFile(config *SandboxConfig) (string, error) {
	data, err := json.MarshalIndent(config, "", "  ")
	if err != nil {
		return "", fmt.Errorf("failed to marshal sandbox config: %w", err)
	}

	tmpFile, err := os.CreateTemp("", "srt-config-*.json")
	if err != nil {
		return "", fmt.Errorf("failed to create temp config file: %w", err)
	}
	defer tmpFile.Close()

	if _, err := tmpFile.Write(data); err != nil {
		os.Remove(tmpFile.Name())
		return "", fmt.Errorf("failed to write sandbox config: %w", err)
	}

	return tmpFile.Name(), nil
}

// CleanupConfigFile removes the config file at the given path, but only if it
// resides within the system temp directory (TMPDIR or /tmp). Returns an error
// from removal; returns nil without removing if the file is outside temp.
//
// Binary address: 0x7dac40
func CleanupConfigFile(configDir string, configPath string) error {
	if configPath == "" {
		return nil
	}

	absPath, err := filepath.Abs(configPath)
	if err != nil {
		return nil
	}

	tmpDir := os.Getenv("TMPDIR")
	if tmpDir == "" {
		tmpDir = "/tmp"
	}

	absTmp, err := filepath.Abs(tmpDir)
	if err != nil {
		return nil
	}

	rel, err := filepath.Rel(absTmp, absPath)
	if err != nil || (len(rel) > 0 && rel[0] == '.') {
		return nil
	}

	if err := os.Remove(configPath); err != nil {
		return fmt.Errorf("failed to remove config file: %w", err)
	}

	return nil
}

// domainMatches checks whether domain matches the pattern.
// Supports wildcard prefix patterns like "*.example.com".
//
// Binary address: 0x7dada0
func domainMatches(pattern string, domain string) bool {
	if len(pattern) == len(domain) {
		if pattern == domain {
			return true
		}
	}

	// Check if pattern starts with "*."
	if len(pattern) >= 2 && pattern[:2] == "*." {
		suffix := pattern[1:] // ".example.com"
		if len(domain) >= len(suffix) {
			if domain[len(domain)-len(suffix):] == suffix {
				return true
			}
		}
	}

	// Check if domain starts with "*."
	if len(domain) >= 2 && domain[:2] == "*." {
		suffix := domain[1:]
		if len(pattern) >= len(suffix) {
			if pattern[len(pattern)-len(suffix):] == suffix {
				return true
			}
		}
	}

	return false
}

// ValidateConfig checks that the sandbox config includes all required
// domains (DefaultAllowedDomains). Returns an error if config is nil or
// a required domain is not covered by any entry in AllowedDomains.
//
// Binary address: 0x7dafa0
func ValidateConfig(config *SandboxConfig) error {
	if config == nil {
		return fmt.Errorf("config is nil")
	}

	// requiredDomains is a compile-time list of domains that must be covered
	// by config.AllowedDomains. The binary stores these as two adjacent string
	// pairs in a stack-allocated array (0x58-0x78(SP)), iterated with index CX.
	requiredDomains := [2]string{
		"api.anthropic.com",
		"*.anthropic.com",
	}

	for _, required := range requiredDomains {
		found := false
		for _, allowed := range config.AllowedDomains {
			if domainMatches(required, allowed) {
				found = true
				break
			}
		}
		if !found {
			return fmt.Errorf("required domain %q not found in allowed domains (have: %v)", required, config.AllowedDomains)
		}
	}

	return nil
}

// LoadAndValidateConfig reads a sandbox config JSON file from disk, parses it,
// and validates the result. Returns the parsed config or an error.
//
// Binary address: 0x7db1a0
func LoadAndValidateConfig(path string) (*SandboxConfig, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, fmt.Errorf("failed to read config file %s: %w", path, err)
	}

	config := &SandboxConfig{}
	if err := json.Unmarshal(data, config); err != nil {
		return nil, fmt.Errorf("failed to parse config file %s: %w", path, err)
	}

	if err := ValidateConfig(config); err != nil {
		return nil, fmt.Errorf("config validation failed: %w", err)
	}

	return config, nil
}
