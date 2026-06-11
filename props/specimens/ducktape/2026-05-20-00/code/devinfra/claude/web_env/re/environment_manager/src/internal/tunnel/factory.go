// Reconstructed factory for tunnel client creation
// This file breaks the circular dependency by having tunnel package
// set the manager.NewTunnelClient variable.

package tunnel

import (
	"log/slog"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/dogmetrics"
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
//
//	0: logger (*slog.Logger)
//	1: ctx (context.Context) - not used by NewClient
//	2: metricsClient (dogmetrics.Client) - DataDog metrics client
//	3: sessionID (string) - used as tunnelID
//	4: apiURL (string) - not directly used
//	5: tunnelEndpoint (string)
//	6: sessionConfig (interface{}) - not directly used
//	7: authToken (string)
//	8: actionRegistry (*actions.Registry) - optional
func newTunnelClientFactory(opts ...interface{}) manager.TunnelClient {
	// Validate minimum parameters
	if len(opts) < 8 {
		return nil
	}

	// Extract parameters with type assertions
	logger, ok := opts[0].(*slog.Logger)
	if !ok || logger == nil {
		return nil
	}

	metricsClient, _ := opts[2].(dogmetrics.Client)

	sessionID, ok := opts[3].(string)
	if !ok {
		return nil
	}

	tunnelEndpoint, ok := opts[5].(string)
	if !ok {
		return nil
	}

	authToken, ok := opts[7].(string)
	if !ok {
		return nil
	}

	// Extract optional action registry
	var registry *actions.Registry
	if len(opts) >= 9 {
		if reg, ok := opts[8].(*actions.Registry); ok {
			registry = reg
		}
	}

	// Create the tunnel client
	client := NewClient(
		logger,
		metricsClient, // dogmetrics.Client (may be nil)
		sessionID,     // tunnelID
		tunnelEndpoint,
		authToken,
		registry,
	)

	// Return as TunnelClient interface
	return client
}
