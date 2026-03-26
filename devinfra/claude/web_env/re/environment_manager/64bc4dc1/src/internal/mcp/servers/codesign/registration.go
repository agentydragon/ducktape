// Package codesign implements the code-sign MCP server for git commit signing.
// It provides a "sign_file" MCP tool that signs file content using a remote
// signing service, designed for use with git's gpg.ssh.program configuration.
//
// Reconstructed from a6f96673 DWARF extraction, carried forward to 64bc4dc1.
// Source path: /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/mcp/servers/codesign/
//
// Key symbols:
//   - codesign.Registration (0x15ac340)
//   - codesign.shouldRegister (0xb0d040)
//   - codesign.configureServer (0xb0cc80)
//   - codesign.init (0xb0cbe0)
//   - codesign.init.func1 (0xb0cc60)
package codesign

import (
	"log/slog"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/mcp"
)

// Registration is the global MCP server registration for the codesign server.
// Set during init() via mcp.NewRegistration.
// Symbol: codesign.Registration (0x15ac340)
var Registration *mcp.ServerRegistration

// init registers the codesign MCP server with the global MCP registry.
//
// Binary address: 0xb0cbe0
// Source file: registration.go
//
// Assembly flow:
//  1. LEA "code-sign" string → name (0xb0cbf7)
//  2. LEA description string "codesign server for signing operations..." (0xb0cc01)
//  3. LEA configureServer func ref (0xb0cc0d)
//  4. LEA init.func1 (shouldRegister wrapper) (0xb0cc19)
//  5. CALL mcp.NewRegistration (0xb0cc25)
//  6. Store result in Registration (0xb0cc40)
func init() {
	Registration = mcp.NewRegistration(
		"code-sign",
		"codesign server for signing operations. It's designed to be used with git's",
		configureServer,
		shouldRegisterWrapper,
	)
}

// shouldRegisterWrapper is the init.func1 closure passed to NewRegistration.
// It delegates to shouldRegister.
//
// Binary address: 0xb0cc60
// Source file: registration.go
//
// Assembly:
//
//	XORL AX, AX    ; returns false (0)
//	XORL BX, BX
//	RET
//
// Note: This returns (nil, nil) - effectively a no-op closure. The actual
// shouldRegister function always returns true.
func shouldRegisterWrapper() bool {
	return shouldRegister()
}

// shouldRegister determines whether the codesign MCP server should be
// registered for the current environment. Always returns true.
//
// Binary address: 0xb0d040
// Source file: registration.go
//
// Assembly (trivial):
//
//	MOVL $0x1, AX   ; return true
//	RET
func shouldRegister() bool {
	return true
}

// configureServer creates and configures a new CodeSignMCPServer instance.
// It initializes the embedded BaseServer and sets up the signing configuration.
//
// Binary address: 0xb0cc80
// Source file: registration.go
//
// Assembly flow:
//  1. Allocates CodeSignMCPServer via runtime.newobject (0xb0cca0)
//  2. Creates BaseServer: sets name "code-sign", version (0xb0ccba)
//  3. Stores logger and configuration on the struct
//  4. Returns as MCPServer interface via itab
func configureServer(logger *slog.Logger, name string, envCfg interface{}, authCtx interface{}, sessionCfg interface{}) (mcp.MCPServer, error) {
	server := &CodeSignMCPServer{
		BaseServer: &mcp.BaseServer{},
		logger:     logger,
	}
	return server, nil
}
