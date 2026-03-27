// Reconstructed from binary: environment-manager (Build ID 495ea204)
// Source: internal/sandbox/runtime.go
// Original path: /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/sandbox/runtime.go

package sandbox

import (
	"context"
	"fmt"
	"log/slog"
	"os"
)

// SandboxRuntime manages the lifecycle and invocation of the sandbox runtime
// binary (srt). It wraps commands to run inside the sandbox, manages the config
// file on disk, and handles cleanup.
//
// Type size: 48 bytes (from eq function comparing offsets 0x00-0x29).
// Struct layout (from type equality function at 0x7dcf40):
//
//	offset 0x00: RuntimePath string  (ptr+len, 16 bytes)
//	offset 0x10: ConfigFile  string  (ptr+len, 16 bytes)
//	offset 0x20: Logger      *slog.Logger (8 bytes)
//	offset 0x28: HasConfig   bool    (1 byte)
//	offset 0x29: CustomConfig bool   (1 byte)
type SandboxRuntime struct {
	RuntimePath  string       // Path to the srt binary
	ConfigFile   string       // Path to the written config file on disk
	Logger       *slog.Logger // Structured logger
	HasConfig    bool         // Whether a config file was installed by us
	CustomConfig bool         // Whether a custom (user-supplied) config was used
}

// NewSandboxRuntimeWithConfig creates a new SandboxRuntime, optionally loading a
// custom sandbox config from configPath, or building a default config with
// standard security rules.
//
// Parameters:
//   - logger: structured logger (passed in DI register at 0x7dbd9d)
//   - configPath: path to custom sandbox config JSON; empty for defaults (SI register at 0x7dbdbd)
//   - runtimePath: override for srt binary path; empty to auto-detect (R8 register at 0x7dbdb5)
//   - extraDenyReadPaths: additional paths to add to allow-write list (R9/R10 registers)
//
// Returns the runtime and an error.
//
// Binary address: 0x7dbd80
func NewSandboxRuntimeWithConfig(
	config *SandboxConfig,
	logger *slog.Logger,
) (*SandboxRuntime, error) {
	// Determine the srt binary path from environment, defaulting to "srt".
	srtBinary := os.Getenv("SRT_BINARY_PATH")

	// Ensure the Claude temp directory exists (0x7dbdf4: os.MkdirAll).
	err := os.MkdirAll("/tmp/claude", 0o755)

	// If TMPDIR env was empty, use default "srt".
	if srtBinary == "" {
		srtBinary = "srt"
	}

	// If MkdirAll failed, log a warning and continue.
	if err != nil {
		if logger != nil {
			logger.Log(context.Background(), slog.LevelWarn,
				"failed to create claude tmp directory",
				"path", "/tmp/claude",
				"error", err,
			)
		}
	}

	hasCustomConfig := config != nil

	if config == nil {
		// Build the default sandbox config.
		config = buildDefaultConfig(nil)
	} else {
		if logger != nil {
			logger.Log(context.Background(), slog.LevelDebug,
				"using custom sandbox config",
				"allowed_domains", config.AllowedDomains,
			)
		}
	}

	// Write the config to a temp file (0x7dc403: WriteConfigFile).
	configFile, err := WriteConfigFile(config)
	if err != nil {
		return nil, fmt.Errorf("failed to write sandbox config file: %w", err)
	}

	// Log the final sandbox runtime state.
	if logger != nil {
		logger.Log(context.Background(), slog.LevelDebug,
			"sandbox runtime initialized",
			"srt_binary", srtBinary,
			"config_path", configFile,
			"custom_config", hasCustomConfig,
		)
	}

	return &SandboxRuntime{
		RuntimePath:  srtBinary,
		ConfigFile:   configFile,
		Logger:       logger,
		HasConfig:    true,
		CustomConfig: hasCustomConfig,
	}, nil
}

// buildDefaultConfig constructs the default SandboxConfig with standard
// security defaults. This is an inlined portion of NewSandboxRuntimeWithConfig
// (0x7dc18d-0x7dc3ff) that allocates and populates the config struct.
//
// The default configuration includes:
//   - Allowed domains: api.anthropic.com, api-staging.anthropic.com, *.anthropic.com
//   - Allow-write paths: /tmp, /tmp/claude, ~, /workspace
//     (plus any extraDenyReadPaths appended to allow-write)
//   - Deny-read paths: ~/.ssh, ~/.aws, ~/.config/gcloud, /etc/shadow, /etc/passwd-, /secrets
func buildDefaultConfig(extraDenyReadPaths []string) *SandboxConfig {
	// Default allow-write paths (4 base entries, capacity 4 or 5 depending on extras).
	// Binary: newobject at 0x7dc18e allocates [4]struct{string,string,string} for allow-write.
	allowWrite := []string{
		"/tmp",        // offset 0x00, len 4
		"/tmp/claude", // offset 0x10, len 11 (0xb)
		"~",           // offset 0x20, len 1
		"/workspace",  // offset 0x30, len 10 (0xa)
	}

	// If extra paths are provided, grow and append them (0x7dc1e5-0x7dc25d).
	if len(extraDenyReadPaths) > 0 {
		allowWrite = append(allowWrite, extraDenyReadPaths...)
	}

	// Default allowed domains (3 entries).
	// Binary: newobject at 0x7dc26f allocates [3]struct{string,string} for domains.
	allowedDomains := []string{
		"api.anthropic.com",         // offset 0x00, len 17 (0x11)
		"api-staging.anthropic.com", // offset 0x10, len 25 (0x19)
		"*.anthropic.com",           // offset 0x20, len 15 (0xf)
	}

	// Default deny-read paths (6 entries).
	// Binary: newobject at 0x7dc2bb allocates [6]struct{string,string,string} for deny paths.
	denyRead := []string{
		"~/.ssh",           // offset 0x00, len 6
		"~/.aws",           // offset 0x10, len 6
		"~/.config/gcloud", // offset 0x20, len 16 (0x10)
		"/etc/shadow",      // offset 0x30, len 11 (0xb)
		"/etc/passwd-",     // offset 0x40, len 12 (0xc)
		"/secrets",         // offset 0x50, len 8
	}

	// Assemble the config. The SandboxConfig struct (136 bytes) is allocated
	// at 0x7dc340 via newobject, then fields are populated.
	config := &SandboxConfig{
		AllowedDomains: allowedDomains,
		DenyRead:       denyRead,
		AllowWrite:     allowWrite,
	}

	return config
}

// WrapCommand prepends the sandbox runtime invocation to the given command and
// arguments. It returns the srt binary path and the full argument slice:
//
//	["--settings", <configFile>, "--", command, args...]
//
// If the SandboxRuntime has a Logger, it logs the wrapping at debug level with
// four slog attributes: original_command, sandbox_binary, original_args, wrapped_args.
//
// Binary address: 0x7dc740
func (s *SandboxRuntime) WrapCommand(command string, args []string) (string, []string) {
	// Build the prefix args (allocated as [4]string at 0x7dc794):
	//   "--settings" (len 10), configFile, "--" (len 2), command
	prefixArgs := []string{
		"--settings", // 0x00(obj): ptr to "--settings", len 10
		s.ConfigFile, // 0x10(obj): from s.ConfigFile
		"--",         // 0x20(obj): ptr to "--", len 2
		command,      // 0x30(obj): from command arg
	}

	// Build the wrapped args slice: prefix + original args.
	// Binary: growslice at 0x7dc84a grows to len(args)+4.
	wrappedArgs := make([]string, 0, len(args)+4)
	wrappedArgs = append(wrappedArgs, prefixArgs...)
	wrappedArgs = append(wrappedArgs, args...)

	// Log the wrapping if a logger is configured (0x7dc8a6: CMP s.Logger != nil).
	if s.Logger != nil {
		s.Logger.Log(context.Background(), slog.LevelDebug,
			"wrapped command for sandbox",
			"original_command", command, // slog attr at 0xd0(SP)
			"sandbox_binary", s.RuntimePath, // slog attr at 0x80(SP)
			"original_args", args, // slog attr at 0xa8(SP)
			"wrapped_args", wrappedArgs, // slog attr at 0x58(SP)
		)
	}

	return s.RuntimePath, wrappedArgs
}

// Cleanup removes the sandbox config file from disk if one was installed.
// If HasConfig is false (custom config was supplied, not generated by us),
// it logs a debug message and skips removal. If HasConfig is true, it calls
// CleanupConfigFile and logs any error.
//
// Binary address: 0x7dcb80
func (s *SandboxRuntime) Cleanup() error {
	// If no config file path, nothing to do (0x7dcba1: TESTQ CX, CX).
	if s.ConfigFile == "" {
		return nil
	}

	if !s.HasConfig {
		// Custom config: we did not create this file, so skip cleanup.
		if s.Logger != nil {
			s.Logger.Log(context.Background(), slog.LevelDebug,
				"skipping cleanup of custom config",
				"config_path", s.ConfigFile,
			)
		}
		return nil
	}

	// Config was installed by us; clean it up (0x7dcc98: CALL CleanupConfigFile).
	if s.Logger != nil {
		s.Logger.Log(context.Background(), slog.LevelDebug,
			"cleaning up sandbox config",
			"config_path", s.ConfigFile,
		)
	}

	err := CleanupConfigFile(s.RuntimePath, s.ConfigFile)
	if err != nil {
		if s.Logger != nil {
			s.Logger.Log(context.Background(), slog.LevelWarn,
				"failed to cleanup sandbox config",
				"config_path", s.ConfigFile,
				"error", err,
			)
		}
		return err
	}

	// Clear the config file reference (0x7dce10-0x7dce2e: zero out ConfigFile).
	s.ConfigFile = ""

	return nil
}
