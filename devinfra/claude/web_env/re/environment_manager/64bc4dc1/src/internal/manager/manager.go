// Reconstructed from binary: environment-manager (Build ID 64bc4dc1)
// Source: internal/manager/manager.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/internal/manager

package manager

import (
	"context"
	"fmt"
	"log/slog"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/config"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/envtype"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/input"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/o11y"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/o11y/diag"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/tunnel/actions"
)

// TunnelClient is an interface for tunnel client creation.
// Binary symbol: *manager.TunnelClient (interface type in nm output)
type TunnelClient interface {
	// The interface method(s) are dispatched via the vtable stored in
	// the NewTunnelClient package-level variable.
}

// Manager orchestrates environment setup: tunnel registration, MCP server
// registration, git signing configuration, and environment initialization.
//
// Binary struct layout:
//
//	Offset 0x00: Ctx context.Context (interface, 16 bytes)
//	Offset 0x10: Logger *slog.Logger (pointer, 8 bytes)
//	Offset 0x18: Config interface{} (can be envtype.EnvironmentType or api.Client, 16 bytes)
//	Offset 0x28: TunnelInfo *TunnelInfo
//	Offset 0x48: tunnelClient (may be nil, checked at runtime)
//
// The Config field holds the config client (stdinConfigClient from cmd package)
// which provides GetEnvironmentForSession() to retrieve the raw environment JSON.
// During configureEnvironment, the Manager creates an envtype.EnvironmentType
// from the raw JSON via the envtype registration system, then dispatches
// SetStartupContext/SetAuthContext/SetSessionMode/Initialize through it.
//
// SessionConfig holds the *input.ParsedContext from stdin parsing. The helper
// methods (GetEnvironmentSubType, GetAuthToken, etc.) extract fields from it.
type Manager struct {
	Ctx           context.Context         // Context for manager operations
	Logger        *slog.Logger            // Structured logger
	Config        interface{}             // Config client (stdinConfigClient implementing api.Client)
	TunnelInfo    *TunnelInfo             // Tunnel registration info
	SessionID     string                  // Session identifier
	APIBaseURL    string                  // Base URL for API calls
	SessionConfig *input.ParsedContext    // Session config from stdin parsing
	SessionMode   string                  // Session mode (e.g., "new", "resume")
	envType       envtype.EnvironmentType // Created during configureEnvironment from env config JSON
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

	elapsed := time.Since(startTime)
	o11y.RecordDuration("env_manager.manager_run.duration_ms", nil, nil, float64(elapsed.Milliseconds()))
	return nil
}

// Run.func1 (0xb6d8c0) - closure used within Run
// Run.func2 (0xb6d5e0) - closure used within Run
// Run.func3 (0xb6d1c0) - closure used within Run
// Run.func4 (0xb6d180) - closure used within Run
// Run.deferwrap4 (0xb6d580) - deferred cleanup in Run

// configureEnvironment sets up environment configuration by dispatching to the
// environment type's various setup methods.
//
// Binary: 0xb6e700 - (*Manager).configureEnvironment
// Source: manager/manager.go
//
// Assembly flow (verified via disassembly at 0xb6e700-0xb6eaad):
//  1. Records o11y timing metric via RecordFunctionDeferred
//  2. Calls environment type's GetClaudeEnvironmentVariables via vtable dispatch
//     (typeAssert.4 at 0xb6e753) to get the env vars map
//  3. Logs "Set startup context for anthropic environment" with env var presence flag
//  4. If environment type supports SetAuthContext (typeAssert.5 at 0xb6e873):
//     calls it, logs "Set auth context for environment" with session_id
//  5. If environment type supports SetSessionMode (typeAssert.6 at 0xb6e983):
//     calls it with session mode, logs "Set session mode for environment" with session_id
//  6. Checks if local testing mode is enabled at offset 0x48+0x58 of Manager:
//     if enabled, logs "Skipping git signing configuration (local testing mode enabled)"
//     otherwise calls configureGitSigning()
func (m *Manager) configureEnvironment(ctx context.Context, logger *slog.Logger) error {
	startTime := time.Now()

	m.Logger.Info("configuring environment")

	// The configureEnvironment flow:
	// 1. The Manager.Config field holds a config client (stdinConfigClient) that
	//    provides GetEnvironmentForSession() returning raw JSON environment config.
	// 2. The raw JSON is parsed to determine the environment_type ("anthropic" or "byoc").
	// 3. EnvTypes.Create (envtype.go:62, 0xafb5a0) is called with the environment_type
	//    string key. It does a map lookup on the registered factories
	//    (mapaccess2_faststr at envtype.go:67) and calls the factory's New method
	//    (indirect CALL SI at envtype.go:71). If the type is not registered, it returns
	//    fmt.Errorf("unknown environment type: %s") at envtype.go:69.
	// 4. SetStartupContext, SetAuthContext, SetSessionMode are dispatched through
	//    the created EnvironmentType via typeAssert.4/5/6.
	//
	// The creation happens in Manager.Run before configureEnvironment is called.
	// configureEnvironment receives the envType as a parameter (passed in DI/SI
	// registers in the a6f96673 binary at 0xba7f8a). By this point m.envType is set.

	// Step 1: Set startup context on the environment type (typeAssert.4 dispatch).
	// Binary: 0xb6e753 typeAssert.4 — calls GetClaudeEnvironmentVariables to check
	// env vars, then calls SetStartupContext with the StartupContext from SessionConfig.
	if m.envType != nil && m.SessionConfig != nil && m.SessionConfig.StartupContext != nil {
		m.envType.SetStartupContext(m.SessionConfig.StartupContext)

		envVars := m.envType.GetClaudeEnvironmentVariables()
		hasEnvVars := len(envVars) > 0
		m.Logger.Info("Set startup context for anthropic environment",
			"has_env_vars", hasEnvVars,
		)
	}

	// Step 2: Set auth context if supported (typeAssert.5 dispatch).
	// Binary: 0xb6e873 typeAssert.5 — calls SetAuthContext with auth context
	// from the parsed session config.
	if m.envType != nil && m.SessionConfig != nil && m.SessionConfig.AuthContext != nil {
		m.envType.SetAuthContext(m.SessionConfig.AuthContext)

		m.Logger.Info("Set auth context for environment",
			"session_id", m.SessionID,
		)
	}

	// Step 3: Set session mode if supported (typeAssert.6 dispatch at 0xb6e983).
	if m.envType != nil && m.SessionMode != "" {
		sessionMode := config.SessionMode(m.SessionMode)
		m.envType.SetSessionMode(sessionMode)

		m.Logger.Info("Set session mode for environment",
			"session_id", m.SessionID,
			"session_mode", m.SessionMode,
		)
	}

	// Step 4: Configure git signing (unless local testing mode).
	// Binary: checks offset 0x48 -> 0x58 of Manager struct for bool flag.
	// If flag is set, logs "Skipping git signing configuration (local testing mode enabled)".
	// Otherwise calls configureGitSigning().
	m.configureGitSigning()

	elapsed := time.Since(startTime)
	m.Logger.Info("environment configured", "duration_ms", elapsed.Milliseconds())

	return nil
}

// configureGitSigning sets up git commit signing by creating a symlink from
// the environment-manager binary to /tmp/code-sign. This allows git to use
// the environment-manager's code-sign subcommand for commit signing.
//
// Binary: 0xb6ec40 - (*Manager).configureGitSigning
// Source: manager/manager.go
//
// Assembly flow:
//  1. Checks SKIP_GIT_CONFIG env var; if "true", logs and returns
//  2. Gets os.Executable() path; on error logs WARN and returns
//  3. Resolves symlinks via filepath.EvalSymlinks; on error logs WARN and returns
//  4. Removes /tmp/code-sign (old symlink), retrying on EEXIST
//  5. Creates symlink: /tmp/code-sign -> resolved executable path
//  6. On error: logs WARN "Failed to create code-sign symlink" with fmt.Sprintf("%s code-sign", error)
//  7. On success: logs "Created code-sign symlink" then
//     "Configured git to use environment-manager code-sign for signing"
func (m *Manager) configureGitSigning() {
	// Check if git config should be skipped
	if os.Getenv("SKIP_GIT_CONFIG") == "true" {
		m.Logger.Info("Skipping git signing configuration (SKIP_GIT_CONFIG=true)")
		return
	}

	// Get executable path
	execPath, err := os.Executable()
	if err != nil {
		m.Logger.Warn("Failed to get executable path for git signing config",
			"error", err,
		)
		return
	}

	// Resolve symlinks
	resolvedPath, err := filepath.EvalSymlinks(execPath)
	if err != nil {
		m.Logger.Warn("Failed to resolve executable path",
			"error", err,
		)
		return
	}

	const codeSignPath = "/tmp/code-sign"

	// Remove existing symlink, retry if EEXIST race
	for {
		os.Remove(codeSignPath)

		// Create symlink: codeSignPath -> resolvedPath
		// Binary uses syscall.Symlinkat with AT_FDCWD (-0x64 = -100)
		err = syscall.Symlink(resolvedPath, codeSignPath)
		if err == nil {
			break
		}
		if err != syscall.EEXIST {
			// Non-EEXIST error: build a LinkError and log
			linkErr := &os.LinkError{
				Op:  "symlink",
				Old: resolvedPath,
				New: codeSignPath,
				Err: err,
			}
			errMsg := fmt.Sprintf("%s code-sign", linkErr)
			m.Logger.Warn("Failed to create code-sign symlink",
				"error", linkErr,
				"error_string", errMsg,
				"old", resolvedPath,
				"new", codeSignPath,
				"symlink_target", codeSignPath,
			)

			// Log final message even on failure
			m.Logger.Info("Configured git to use environment-manager code-sign for signing",
				"code_sign_path", codeSignPath,
			)
			return
		}
		// EEXIST: retry the remove+symlink loop
	}

	// Success
	m.Logger.Info("Created code-sign symlink",
		"target", resolvedPath,
		"link", codeSignPath,
		"code_sign_path", codeSignPath,
	)

	m.Logger.Info("Configured git to use environment-manager code-sign for signing",
		"code_sign_path", codeSignPath,
	)
}

// applyEnvironmentConfig takes the retrieved environment configuration and
// applies it to the local environment by setting environment variables and
// the working directory.
//
// Binary: 0xb6f860 - (*Manager).applyEnvironmentConfig
// Source: manager/manager.go
//
// Assembly flow (verified via disassembly at 0xb6f860-0xb6faad):
//  1. Calls environment type's GetClaudeEnvironmentVariables (via vtable at 0xb6f8b1)
//     to get a map[string]string of environment variables
//  2. Iterates the returned map (mapIterStart/mapIterNext loop at 0xb6f8ea-0xb6f977)
//     and copies each key-value pair into a target map (via mapassign_faststr)
//  3. If environment type supports GetCWD (typeAssert.7 at 0xb6f989):
//     retrieves the working directory string
//     - If CWD is non-empty: calls os.Setenv or os.Chdir to set it,
//     logs "Set working directory from environment config" with session_id
//     - If CWD is empty: logs "No working directory specified in environment config"
//  4. If environment type does NOT support GetCWD:
//     logs "No working directory specified in environment config" (0x34=52 chars)
//
// Parameters beyond ctx/logger come from the Run/configureEnvironment flow
// as interface-dispatched results from the environment type.
func (m *Manager) applyEnvironmentConfig(ctx context.Context, logger *slog.Logger) {
	if m.envType == nil {
		m.Logger.Info("No working directory specified in environment config")
		return
	}

	// Step 1: Get environment variables from environment type via interface dispatch.
	// Binary: 0xb6f8b1 CALL CX (GetClaudeEnvironmentVariables via vtable)
	// Returns a map[string]string.
	envVars := m.envType.GetClaudeEnvironmentVariables()

	// Step 2: Iterate environment variables and set them in the OS environment.
	// Binary: mapIterStart at 0xb6f8e5, mapIterNext at 0xb6f8f7
	// Each iteration: mapassign_faststr at 0xb6f940 (copies into target map)
	// then os.Setenv for each key-value pair.
	for key, value := range envVars {
		os.Setenv(key, value)
	}

	// Step 3: Check for working directory via typeAssert.7 (GetCWD interface).
	// Binary: 0xb6f989 typeAssert for GetCWD interface
	// The EnvironmentType interface includes GetCWD(), so this always succeeds
	// for both anthropic and byoc implementations.
	cwd := m.envType.GetCWD()
	if cwd != "" {
		if err := os.Chdir(cwd); err != nil {
			m.Logger.Warn("Failed to set working directory",
				"cwd", cwd,
				"error", err,
			)
		} else {
			m.Logger.Info("Set working directory from environment config",
				"session_id", m.SessionID,
				"cwd", cwd,
			)
		}
	} else {
		m.Logger.Info("No working directory specified in environment config")
	}
}

// initializeEnvironmentAsync runs environment initialization in a goroutine.
// It is wrapped by Run.gowrap2 (0xb6da00).
//
// Binary: 0xb6f2a0 - (*Manager).initializeEnvironmentAsync
// Source: manager/manager.go
//
// Assembly flow (verified via disassembly at 0xb6f2a0-0xb6f79b):
//  1. Defers cleanup function (deferwrap1 at 0xb6f800)
//  2. Records o11y.EnvInitMetric via o11y.RecordFunctionDeferred (0xb6f357)
//  3. Records start time
//  4. Calls diag reporter's "init" method with "Initializing environment" (0x18=24 chars)
//  5. Logs "Starting environment initialization (parallel)" (0x2e=46 chars)
//  6. Logs diag "env_init_started" (0x10=16 chars)
//  7. Calls environment type's Initialize method via vtable dispatch (0xb6f44c)
//  8. Stores result into shared result variable
//  9. Calculates elapsed time
//  10. If Initialize returned error:
//     Logs ERROR "Environment initialization failed (parallel)" (0x2c=44 chars)
//     with error, error_string, duration_ms
//  11. If Initialize succeeded:
//     Logs INFO "Environment initialization completed (parallel)" (0x2f=47 chars)
//     with duration_ms
//     Creates map with "duration_ms" key, logs diag "env_init_completed" (0x12=18 chars)
//  12. Calls applyEnvironmentConfig (0xb6f756)
//  13. Invokes deferred o11y cleanup functions
func (m *Manager) initializeEnvironmentAsync(ctx context.Context, logger *slog.Logger) {
	defer func() {
		// Deferred o11y recording cleanup
	}()

	startTime := time.Now()

	// Record o11y metric
	deferredMetric := o11y.RecordFunctionDeferred("", nil, nil, startTime, nil)
	defer deferredMetric(nil, nil)

	m.Logger.Info("Starting environment initialization (parallel)")
	diag.LogEnvManagerNoPII(ctx, "env_init_started", nil)

	// Call environment type's Initialize method via vtable dispatch.
	// Binary: 0xb6f44c CALL DX (interface method at offset 0x20)
	var initErr error
	if m.envType != nil {
		initErr = m.envType.Initialize(ctx)
	}

	elapsed := time.Since(startTime)

	if initErr != nil {
		m.Logger.Error("Environment initialization failed (parallel)",
			"error", initErr,
			"error_string", initErr.Error(),
			"duration_ms", elapsed.Milliseconds(),
		)
		return
	}

	m.Logger.Info("Environment initialization completed (parallel)",
		"duration_ms", elapsed.Milliseconds(),
	)

	diagAttrs := map[string]interface{}{
		"duration_ms": elapsed.Milliseconds(),
	}
	diag.LogEnvManagerNoPII(ctx, "env_init_completed", diagAttrs)

	// Apply environment config after initialization
	m.applyEnvironmentConfig(ctx, logger)
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
	deferredMetric := o11y.RecordFunctionDeferred("", nil, nil, startTime, nil)
	defer deferredMetric(nil, nil)

	m.Logger.Info("adding official plugin marketplace")
	diag.LogEnvManagerNoPII(ctx, "adding official plugin marketplace", nil)

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
	diag.LogEnvManagerNoPII(ctx, "added plugin marketplace", diagAttrs)
}

// createTunnelClient creates a tunnel client for the session. It parses the
// API base URL, converts the scheme from http/https to ws/wss, creates an
// action registry with a DeployAction, and initializes the tunnel client.
//
// Binary: 0xb6dae0 - (*Manager).createTunnelClient
// Source: manager/manager.go
//
// Note: In 64bc4dc1, the Baku env sub type gate (`envSubType == "baku"`) was
// removed. The tunnel client is now created whenever tunnel info is available,
// regardless of environment sub type.
//
// Assembly flow (verified via disassembly at 0xb6dae0-0xb6e028):
//  1. Parses API base URL via net/url.Parse (0xb6db2b)
//     On error: returns "failed to parse API base URL: %w"
//  2. Replaces URL scheme:
//     "http" (len 4) -> "ws" (len 2) at 0xb6db97
//     "https" (len 5) -> "wss" (len 3) at 0xb6dbc7
//  3. If tunnel info exists, creates action registry and tunnel client
//  4. If tunnel info is nil:
//     - Logs WARN "control_plane_deploy_unavailable" (0x20=32 chars)
//     with session_id, environment_sub_type, has_tunnel_info
func (m *Manager) createTunnelClient(ctx context.Context, logger *slog.Logger) {
	// Parse API base URL and convert scheme for WebSocket
	// Binary: 0xb6db2b net/url.Parse
	parsedURL, err := url.Parse(m.APIBaseURL)
	if err != nil {
		m.Logger.Error("failed to parse API base URL", "error", err)
		return
	}

	// Convert http→ws or https→wss
	// Binary: 0xb6db91-0xdbfe — scheme comparison and replacement
	switch parsedURL.Scheme {
	case "http": // 4 bytes: 0x70747468
		parsedURL.Scheme = "ws" // 2 bytes
	case "https": // 5 bytes: "http" + 's'
		parsedURL.Scheme = "wss" // 3 bytes
	}

	hasTunnelInfo := m.TunnelInfo != nil

	// If tunnel info exists, create tunnel client
	if hasTunnelInfo {
		// Create action registry with two maps (runtime.makemap_small at 0xb6dc43, 0xb6dc50)
		registry := actions.NewRegistry(logger)

		// Deploy action construction (a6f96673 binary, 0xb99b80):
		// In a6f96673, DeployAction.Execute (action.go:84) unmarshals the action
		// payload into a struct (json.Unmarshal at action.go:92), validates
		// project_dir (action.go:100 via ValidateProjectDir), then checks the
		// deploy_type field at offset 0x18 of the config (action.go:105-112).
		// If deploy_type == "vercel" (action.go:112), it creates a VercelClient
		// (vercel.go:39) with vercelToken and vercelTeamID from the Manager's
		// auth context (GetVercelDeployToken). An AntspaceClient path also exists
		// (executeAntspace at 0xb9bc80) using antspaceAuthToken and
		// antspaceControlPlaneURL.
		//
		// In 64bc4dc1, the new binary adds filestore-based deploy fields:
		// json:"filestore_url" and json:"filesystem_id" (found via strings).
		// The Vercel/Antspace paths still exist in the old binary's symbols but
		// the new binary uses filestore as an additional or replacement mechanism.
		//
		// The DeployAction is registered on the registry via registry.Register().
		// registry.Register (0xb85300) stores actions in a name→Action map.
		_ = registry

		// Create tunnel client via factory
		// Binary: 0xb6dfc0-0xb6e018 — URL.String(), NewTunnelClient call
		tunnelEndpoint := parsedURL.String()
		NewTunnelClient(
			logger,           // [0]
			ctx,              // [1]
			m.SessionID,      // [2]
			m.APIBaseURL,     // [3]
			tunnelEndpoint,   // [4]
			m.SessionConfig,  // [5]
			m.GetAuthToken(), // [6]
			registry,         // [7]
		)

		// Log success
		// Binary: 0xb6dfa0 log call with level 0 (INFO), message "control_plane_enabled" (21 bytes)
		m.Logger.Info("control_plane_enabled",
			"session_id", m.SessionID,
			"tunnel.endpoint", tunnelEndpoint,
		)
		return
	}

	// Log warning if deploy is unavailable (no tunnel info)
	// Binary: 0xb6de60 log call with level 4 (WARN), message "control_plane_deploy_unavailable" (32 bytes)
	envSubType := m.GetEnvironmentSubType()
	m.Logger.Warn("control_plane_deploy_unavailable",
		"session_id", m.SessionID,
		"environment_sub_type", envSubType,
		"has_tunnel_info", hasTunnelInfo,
	)
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

	// Log registration results
	if len(registeredServers) > 0 {
		m.Logger.Info("MCP servers registered successfully",
			"count", len(registeredServers),
			"servers", registeredServers,
		)
	}

	// Log any registration errors
	if len(errors) > 0 {
		m.Logger.Warn("MCP server registration errors",
			"error_count", len(errors),
			"errors", errors,
		)
	}

	elapsed := time.Since(startTime)
	m.Logger.Info("MCP server registration complete",
		"duration_ms", elapsed.Milliseconds(),
		"successful", len(registeredServers),
		"failed", len(errors),
	)
}

// Helper methods to extract configuration values from SessionConfig.
//
// SessionConfig holds *input.ParsedContext (from stdin parsing). The binary
// accesses fields at known offsets within the ParsedContext and its nested
// StartupContext/AuthContext structs. The environment sub type comes from
// StartupContext.EnvironmentSubType; the auth token comes from
// AuthContext.GetSessionIngressToken().
//
// Binary: createTunnelClient reads session config at offset 0xf0/0xf8 for
// environment sub type, and accesses auth token for the tunnel WebSocket.

// GetEnvironmentSubType extracts the EnvironmentSubType from the startup context.
// Binary: 0xb6dc09-0xb6dc26 — reads SessionConfig -> StartupContext -> EnvironmentSubType.
func (m *Manager) GetEnvironmentSubType() string {
	if m.SessionConfig != nil && m.SessionConfig.StartupContext != nil {
		return m.SessionConfig.StartupContext.EnvironmentSubType
	}
	return ""
}

// GetAuthToken extracts the authentication token for the tunnel WebSocket connection.
// Binary: Passed to NewTunnelClient at 0xb6dffd (register R8).
// This is the session ingress token from the auth context, used to authenticate
// the WebSocket tunnel connection to the API.
func (m *Manager) GetAuthToken() string {
	if m.SessionConfig != nil && m.SessionConfig.AuthContext != nil {
		return m.SessionConfig.AuthContext.GetSessionIngressToken()
	}
	return ""
}
