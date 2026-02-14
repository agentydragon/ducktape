// Reconstructed from binary: environment-manager (Build ID 6b49f1ca)
// Source: internal/manager/manager.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/internal/manager

package manager

import (
	"context"
	"fmt"
	"log/slog"
	"os/exec"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/mcp"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/o11y"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/o11y/diag"
)

// TunnelClient is an interface for tunnel client creation.
// Binary symbol: *manager.TunnelClient (interface type in nm output)
type TunnelClient interface {
	// The interface method(s) are dispatched via the vtable stored in
	// the NewTunnelClient package-level variable.
}

// Manager orchestrates environment setup: tunnel registration, MCP server
// registration, git signing configuration, and environment initialization.
type Manager struct {
	// Offset 0x00: context or API client (interface pair)
	// Offset 0x10: logger *slog.Logger
	// Offset 0x18: session config / environment config client (interface pair)
	// Offset 0x28: ...
	// Offset 0x48: tunnelClient (may be nil, checked at runtime)
	Logger     *slog.Logger
	Config     interface{} // environment config client
	TunnelInfo *TunnelInfo
}

// TunnelInfo holds tunnel client data used during registration.
type TunnelInfo struct {
	PluginMarketplaceURL string
	PluginMarketplaceLen int
}

// Run is the main entry point for the Manager. It records timing via o11y,
// starts async goroutines for MCP registration and tunnel registration,
// configures the environment, initializes it, and adds the official plugin
// marketplace.
//
// Binary: 0xb6a360 - (*Manager).Run
// Source: manager/manager.go
func (m *Manager) Run(ctx context.Context, logger *slog.Logger) error {
	startTime := time.Now()

	// Log the start of the run with context info.
	// Observed: slog.(*Logger).log calls at 0xb6a401+ with level 0 (Info)
	m.Logger.Info("starting manager run")

	// Launch async goroutines for parallel work:
	//   gowrap1 (0xb6da60): registerMCPServersAsync
	//   gowrap2 (0xb6da00): initializeEnvironmentAsync
	//   gowrap3 (0xb6d980): addOfficialPluginMarketplaceAsync
	go m.registerMCPServersAsync(ctx, logger)
	go m.initializeEnvironmentAsync(ctx, logger)
	go m.addOfficialPluginMarketplaceAsync(ctx, logger)

	// Configure the environment synchronously.
	// Binary: call at ~0xb6a500+ to configureEnvironment
	if err := m.configureEnvironment(ctx, logger); err != nil {
		return fmt.Errorf("failed to configure environment: %w", err)
	}

	// Create tunnel client if needed.
	// Binary: call at ~0xb6a600+ to createTunnelClient
	m.createTunnelClient(ctx, logger)

	_ = startTime
	return nil
}

// Run.func1 (0xb6d8c0) - closure used within Run
// Run.func2 (0xb6d5e0) - closure used within Run
// Run.func3 (0xb6d1c0) - closure used within Run
// Run.func4 (0xb6d180) - closure used within Run
// Run.deferwrap4 (0xb6d580) - deferred cleanup in Run

// configureEnvironment sets up environment configuration by calling the config
// client and applying settings.
//
// Binary: 0xb6e700 - (*Manager).configureEnvironment
// Source: manager/manager.go
func (m *Manager) configureEnvironment(ctx context.Context, logger *slog.Logger) error {
	startTime := time.Now()

	// Calls o11y.RecordFunctionDeferred for metrics tracking
	// Then calls the config client to get environment configuration
	// Applies the returned config via applyEnvironmentConfig

	m.Logger.Info("configuring environment")

	if err := m.applyEnvironmentConfig(ctx, logger); err != nil {
		return fmt.Errorf("failed to apply environment config: %w", err)
	}

	elapsed := time.Since(startTime)
	m.Logger.Info("environment configured", "duration_ms", elapsed.Milliseconds())

	return nil
}

// configureGitSigning sets up git commit signing by configuring gpg or SSH
// signing with the provided key material.
//
// Binary: 0xb6ec40 - (*Manager).configureGitSigning
// Source: manager/manager.go
func (m *Manager) configureGitSigning(ctx context.Context, logger *slog.Logger) error {
	startTime := time.Now()

	// Calls o11y metric recording
	// Executes git config commands for signing setup
	// Logs the result

	m.Logger.Info("configuring git signing")

	elapsed := time.Since(startTime)
	m.Logger.Info("git signing configured", "duration_ms", elapsed.Milliseconds())

	return nil
}

// applyEnvironmentConfig takes the retrieved environment configuration and
// applies it to the local environment (env vars, git config, etc.).
//
// Binary: 0xb6f860 - (*Manager).applyEnvironmentConfig
// Source: manager/manager.go
func (m *Manager) applyEnvironmentConfig(ctx context.Context, logger *slog.Logger) error {
	// Applies configuration settings to the environment
	// This includes setting environment variables and writing config files
	return nil
}

// initializeEnvironmentAsync runs environment initialization in a goroutine.
// It is wrapped by Run.gowrap2 (0xb6da00).
//
// Binary: 0xb6f2a0 - (*Manager).initializeEnvironmentAsync
// Source: manager/manager.go
func (m *Manager) initializeEnvironmentAsync(ctx context.Context, logger *slog.Logger) {
	// deferwrap1 at 0xb6f800 handles deferred cleanup
	defer func() {
		// Deferred o11y recording
	}()

	startTime := time.Now()

	// Calls o11y.RecordFunctionDeferred for EnvironmentInitMetric
	m.Logger.Info("initializing environment async")

	// Performs async environment initialization tasks

	elapsed := time.Since(startTime)
	m.Logger.Info("environment initialization complete", "duration_ms", elapsed.Milliseconds())
}

// addOfficialPluginMarketplaceAsync registers the official VS Code plugin
// marketplace by running `npm config set registry <url>` or similar.
// It is wrapped by Run.gowrap3 (0xb6d980).
//
// Binary: 0xb6fb40 - (*Manager).addOfficialPluginMarketplaceAsync
// Source: manager/manager.go
func (m *Manager) addOfficialPluginMarketplaceAsync(ctx context.Context, logger *slog.Logger) {
	// deferwrap1 at 0xb70220 handles deferred cleanup
	defer func() {
		// Deferred o11y recording
	}()

	startTime := time.Now()

	// Records o11y.PluginMarketplaceMetric via o11y.RecordFunctionDeferred (0xb6fbd2)
	o11y.RecordFunctionDeferred(ctx, logger, o11y.PluginMarketplaceMetric, nil)

	m.Logger.Info("adding official plugin marketplace")
	diag.LogEnvManagerNoPII(ctx, logger, "adding official plugin marketplace", nil)

	// Gets the NPM registry URL from env var or uses default "stable"
	// Binary: os.Getenv at 0xb6fc69 for "NPM_CONFIG_REGISTRY" (len 0x13=19)
	// Default fallback is "stable" (len 6)
	// Checks m.TunnelInfo at offset 0x48 for override URL

	// Constructs command: npm config set registry <url> --location=global ...
	// Binary: CommandContext call at 0xb6fd6d with 4 args
	// Args observed: "npm", "config", "set", <registry URL with 0x39=57 chars>
	output, err := exec.CommandContext(ctx, "npm", "config", "set", "registry", "--location=global").CombinedOutput()

	elapsed := time.Since(startTime)

	if err != nil {
		// Error path at 0xb6fdc3: fmt.Errorf with error + output
		logger.Error("failed to add official plugin marketplace",
			"error", err,
			"output", string(output),
			"duration_ms", elapsed.Milliseconds(),
		)
		return
	}

	// Success path at 0xb70021: logs success with duration
	logger.Info("official plugin marketplace added successfully",
		"duration_ms", elapsed.Milliseconds(),
	)

	// Also logs to diag with a map containing the duration
	diagAttrs := map[string]interface{}{
		"duration_ms": elapsed.Milliseconds(),
	}
	diag.LogEnvManagerNoPII(ctx, logger, "added plugin marketplace", diagAttrs)
}

// createTunnelClient creates a tunnel client for the session using the
// NewTunnelClient factory function (set via init.0).
//
// Binary: 0xb6dae0 - (*Manager).createTunnelClient
// Source: manager/manager.go
func (m *Manager) createTunnelClient(ctx context.Context, logger *slog.Logger) {
	// Uses the package-level NewTunnelClient variable (set in init.0)
	// to create a tunnel client instance
	m.Logger.Info("creating tunnel client")
}

// registerMCPServersAsync wraps registerMCPServers for goroutine execution.
// It is wrapped by Run.gowrap1 (0xb6da60).
//
// Binary: 0xb6e080 - (*Manager).registerMCPServersAsync
// Source: manager/manager.go
func (m *Manager) registerMCPServersAsync(ctx context.Context, logger *slog.Logger) {
	// deferwrap1 at 0xb6e6a0 handles deferred cleanup
	defer func() {
		// Deferred o11y recording
	}()

	startTime := time.Now()

	m.Logger.Info("registering MCP servers async")

	registeredServers, errors := m.registerMCPServers(ctx, logger)
	_ = registeredServers
	_ = errors

	elapsed := time.Since(startTime)
	m.Logger.Info("MCP server registration complete", "duration_ms", elapsed.Milliseconds())
}
