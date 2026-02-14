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
// Assembly flow (verified via disassembly at 0xb70ea0-0xb70fe5):
//  1. Receives many register params from createTunnelClient:
//     AX=logger, BX=ctx, CX=sessionID, DI=API URL, SI=tunnel endpoint,
//     R8=session config, R9=auth token, R10=action registry, R11=has registry flag
//  2. If R11 (action registry) is non-nil:
//     Creates a closure (WithActionRegistry.1 at 0xb71060) that captures R11 (the registry)
//     Allocates closure struct via runtime.newobject (0xb70f2b)
//     Stores function pointer and captured registry in closure
//  3. Calls tunnel.NewClient (0xb70fd0) with:
//     - All the forwarded params (logger, ctx, sessionID, API URL, etc.)
//     - Options slice containing the WithActionRegistry closure (if any)
//     - DX=4 (options count/type), R11=1 (option present flag)
//  4. Returns result as TunnelClient interface:
//     AX = itab for *tunnel.Client → TunnelClient
//     BX = *tunnel.Client pointer
func defaultNewTunnelClient(opts ...interface{}) TunnelClient {
	// This function is called from createTunnelClient with many parameters.
	// It creates a tunnel.Client by forwarding all params to tunnel.NewClient,
	// optionally wrapping the action registry in a WithActionRegistry option closure.
	//
	// The actual tunnel.NewClient call (0xb70fd0) creates and returns a
	// *tunnel.Client which is wrapped in the TunnelClient interface.
	//
	// tunnel.NewClient signature (from binary):
	//   func NewClient(opts ...ClientOption) *Client
	return nil // Stub: actual implementation delegates to tunnel.NewClient
}

// mcp import is used for GetMCPRegistrations in the registerMCPServers flow.
var _ = mcp.GetMCPRegistrations
