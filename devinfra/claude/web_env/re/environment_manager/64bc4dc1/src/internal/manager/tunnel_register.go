// Reconstructed from binary: environment-manager (Build ID 64bc4dc1)
// Source: internal/manager/tunnel_register.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/internal/manager

package manager

import (
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/mcp"
)

// NewTunnelClient is a package-level variable holding the factory function for
// creating tunnel clients. It is set by the tunnel package's init function
// (in internal/tunnel/factory.go) to break the circular dependency.
//
// Binary: 0x15ac038 (D) - global variable
// Source: tunnel_register.go
//
// The factory function receives parameters via the opts slice:
//
//	0: logger (*slog.Logger)
//	1: ctx (context.Context)
//	2: sessionID (string)
//	3: apiURL (string)
//	4: tunnelEndpoint (string)
//	5: sessionConfig (interface{})
//	6: authToken (string)
//	7: actionRegistry (*actions.Registry) - optional
//
// The actual implementation is in internal/tunnel/factory.go, which avoids
// importing tunnel from manager (would create a circular dependency).
var NewTunnelClient func(opts ...interface{}) TunnelClient

// mcp import is used for GetMCPRegistrations in the registerMCPServers flow.
var _ = mcp.GetMCPRegistrations
