// Reconstructed factory for tunnel client creation
// This file breaks the circular dependency by having tunnel package
// set the manager.NewTunnelClient variable.

package tunnel

import (
	"log/slog"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/manager"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/tunnel/actions"
)

func init() {
	// Set the factory function in the manager package.
	// This allows manager to create tunnel clients without importing tunnel.
	manager.NewTunnelClient = newTunnelClientFactory
}

// newTunnelClientFactory is the factory function for creating tunnel clients.
// It extracts parameters from the opts slice and calls NewClient.
//
// Expected parameters in opts (matching binary calling convention):
//   0: logger (*slog.Logger)
//   1: ctx (context.Context) - not used by NewClient
//   2: sessionID (string) - used as metricsKey and tunnelID
//   3: apiURL (string) - not directly used
//   4: tunnelEndpoint (string)
//   5: sessionConfig (interface{}) - not directly used
//   6: authToken (string)
//   7: actionRegistry (*actions.Registry) - optional
func newTunnelClientFactory(opts ...interface{}) manager.TunnelClient {
	// Validate minimum parameters
	if len(opts) < 7 {
		return nil
	}

	// Extract parameters with type assertions
	logger, ok := opts[0].(*slog.Logger)
	if !ok || logger == nil {
		return nil
	}

	sessionID, ok := opts[2].(string)
	if !ok {
		return nil
	}

	tunnelEndpoint, ok := opts[4].(string)
	if !ok {
		return nil
	}

	authToken, ok := opts[6].(string)
	if !ok {
		return nil
	}

	// Extract optional action registry
	var registry *actions.Registry
	if len(opts) >= 8 {
		if reg, ok := opts[7].(*actions.Registry); ok {
			registry = reg
		}
	}

	// Create the tunnel client
	// Use sessionID as both metricsKey and tunnelID (common pattern in the codebase)
	client := NewClient(
		logger,
		sessionID,      // metricsKey
		sessionID,      // tunnelID
		tunnelEndpoint,
		authToken,
		registry,
	)

	// Return as TunnelClient interface
	return client
}
