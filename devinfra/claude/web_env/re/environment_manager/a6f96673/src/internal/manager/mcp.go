// Reconstructed from binary: environment-manager (Build ID a6f96673)
// Source: internal/manager/mcp.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/internal/manager

package manager

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/mcp"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/o11y/diag"
)

// MCPRegistrationResult holds the result of registering a single MCP server.
type MCPRegistrationResult struct {
	Server  *mcp.ServerRegistration
	SockDir string // path to the socket directory
}

// registerMCPServers iterates over all MCP registrations from the session config,
// calls setupMCPServerWithRegistration for each, and returns the list of
// successfully registered servers plus any errors.
//
// Binary: 0xb70280 - (*Manager).registerMCPServers
// Source: manager/mcp.go
//
// Parameters (from register ABI):
//
//	AX = *Manager (self)
//	BX, CX = ctx (context.Context interface pair)
//	DI = logger *slog.Logger (or related)
//	SI, R8, R9, R10 = additional session/config params
//
// Returns:
//
//	AX = []MCPRegistrationResult (data ptr)
//	BX = len
//	CX = cap
//	DI = []error (data ptr)
//	SI = len(errors)
//	R8 = cap(errors)
//	R9, R10 = zero (unused)
func (m *Manager) registerMCPServers(
	ctx context.Context,
	logger *slog.Logger,
) ([]MCPRegistrationResult, []error) {
	startTime := time.Now()

	// Get MCP registrations from the session config.
	// Binary: 0xb70315 call to mcp.GetMCPRegistrations
	// m.Config.Session.MCPRegistrations or similar - accessed via offset 0x18 -> 0x20
	// m.TunnelInfo at offset 0x48
	registrations := mcp.GetMCPRegistrations( /* envType */ "" /* sessionMode */, "")
	totalCount := len(registrations)

	m.Logger.Info("registering MCP servers", "count", totalCount)

	var results []MCPRegistrationResult
	var errors []error

	// Iterate over each registration.
	// Binary: loop from 0xb703e1 to 0xb70666, incrementing index at 0xb703d6
	for i := 0; i < totalCount; i++ {
		reg := registrations[i]

		// Call setupMCPServerWithRegistration for each.
		// Binary: 0xb70462 call to setupMCPServerWithRegistration
		result, err := m.setupMCPServerWithRegistration(ctx, logger, reg)
		if err != nil {
			// Error case at 0xb7051c:
			// Log the error with diag.LogEnvManagerNoPII
			// "failed to register MCP server" (len 0x20=32)
			// Log with slog at ERROR level (0x08) with server name, error, error string, duration
			m.Logger.Error("failed to register MCP server",
				"server", reg.Name(),
				"error", err,
			)
			diag.LogEnvManagerNoPII(ctx, "failed to register MCP server", nil)
			continue
		}

		if result != nil {
			// Success: append to results
			results = append(results, *result)
		}
	}

	elapsed := time.Since(startTime)

	// Log completion summary.
	// Binary: 0xb70666+ slog call with 6 attrs: count, duration, registered, errors, total
	m.Logger.Info("MCP server registration finished",
		"duration_ms", elapsed.Milliseconds(),
		"registered", len(results),
		"errors", len(errors),
		"total", totalCount,
	)

	return results, errors
}

// setupMCPServerWithRegistration sets up a single MCP server using the
// registration config. It calls the underlying MCP setup function and
// returns the result or an error.
//
// Binary: 0xb70860 - (*Manager).setupMCPServerWithRegistration
// Source: manager/mcp.go
//
// Parameters (from register ABI):
//
//	AX = *Manager (self)
//	BX, CX = ctx (context.Context interface pair)
//	DI = logger *slog.Logger
//	SI = MCP registration socket path
//	R8, R9, R10, R11 = additional registration params
//
// Returns:
//
//	AX = *MCPRegistrationResult (nil on error)
//	BX = sockDir string
//	CX = error (interface type ptr, nil on success)
//	DI = error (interface data ptr)
func (m *Manager) setupMCPServerWithRegistration(
	ctx context.Context,
	logger *slog.Logger,
	reg *mcp.ServerRegistration,
) (*MCPRegistrationResult, error) {
	startTime := time.Now()

	// Log with 4 slog attrs: "name", <name>, "command", <command>
	// Binary: 0xb709e0 slog.(*Logger).log call
	m.Logger.Info("setting up MCP server",
		"name", reg.Name(),
		"command", reg.Command(),
	)

	// Call the registration's Setup method via vtable dispatch.
	// Binary: 0xb70a46 CALL R12 (indirect call through registration's method table)
	// The setup function at reg offset 0x10 -> 0x00 (method pointer)
	// Passes: m.Logger, ctx, socketPath, and registration params
	result, sockDir, err := reg.Setup(ctx, m.Logger)

	elapsed := time.Since(startTime)

	if err != nil {
		// Error path at 0xb70a83:
		// Logs error with 6 slog attrs at ERROR level (0x08):
		// "name", "error", "error_string", "duration_ms"
		m.Logger.Error("MCP server setup failed",
			"name", reg.Name(),
			"command", reg.Command(),
			"duration_ms", elapsed.Milliseconds(),
			"error", err,
		)

		return nil, fmt.Errorf("failed to set up MCP server %s: %w", reg.Name(), err)
	}

	// Success path at 0xb70c82:
	// Logs with 6 slog attrs: "name", "command", "duration_ms"
	m.Logger.Info("MCP server setup complete",
		"name", reg.Name(),
		"command", reg.Command(),
		"duration_ms", elapsed.Milliseconds(),
	)

	_ = result
	return &MCPRegistrationResult{
		Server:  reg,
		SockDir: sockDir,
	}, nil
}
