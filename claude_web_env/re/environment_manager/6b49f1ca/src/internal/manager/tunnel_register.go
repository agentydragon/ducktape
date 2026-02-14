// Reconstructed from binary: environment-manager (Build ID 6b49f1ca)
// Source: internal/manager/tunnel_register.go
// Package: github.com/anthropics/anthropic/api-go/environment-manager/internal/manager

package manager

import (
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/mcp"
)

// NewTunnelClient is a package-level variable holding the factory function for
// creating tunnel clients. It is set in init.0 to a default implementation
// (init.0.func1) that uses WithActionRegistry.
//
// Binary: 0x15ac038 (D) - global variable
// Source: tunnel_register.go
var NewTunnelClient func(opts ...interface{}) TunnelClient

// init.0 initializes the NewTunnelClient package variable with the default
// factory function (init.0.func1).
//
// Binary: 0xb70e60 - manager.init.0
// Source: tunnel_register.go
//
// Disassembly shows:
//   - Loads the address of init.0.func1 (0xb70ea0) via LEAQ
//   - Stores it into NewTunnelClient (0x15ac038) global variable
//   - Uses write barrier for GC safety
func init() {
	NewTunnelClient = defaultNewTunnelClient
}

// defaultNewTunnelClient is the default factory function for creating tunnel
// clients. It corresponds to init.0.func1.
//
// Binary: 0xb70ea0 - manager.init.0.func1
// Source: tunnel_register.go
//
// This function creates a new tunnel client with the MCP action registry.
// It delegates to init.0.func1.WithActionRegistry.1 (0xb71060).
func defaultNewTunnelClient(opts ...interface{}) TunnelClient {
	// Binary: init.0.func1.WithActionRegistry.1 at 0xb71060
	// Sets up the tunnel client with the MCP action registry
	return nil
}

// mcp import is used for GetMCPRegistrations in the registerMCPServers flow.
var _ = mcp.GetMCPRegistrations
