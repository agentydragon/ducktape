// Reconstructed from a6f96673 DWARF extraction, carried forward to 495ea204.
// Source: internal/mcp/servers/codesign/types.go
//
// Original source path:
//   /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/mcp/servers/codesign/types.go
//
// This file contains type definitions for the codesign MCP server.
// The file exists in the old binary's DWARF paths but has no TEXT symbols
// (type definitions only, all inlined by the compiler).
//
// Key types:
//   - RemoteSignRequest (type:.eq at 0x8cc2a0) — moved to sign_operations.go
//   - RemoteSignResponse — response from the signing service
//
// The RemoteSignRequest struct is defined in sign_operations.go (where it
// was reconstructed from the executeSign disassembly). This file contains
// the remaining type definitions used by the codesign server.

package codesign

// RemoteSignResponse is the response payload from the remote signing service.
//
// Reconstructed from signContent/executeSign response handling at 0xb10be6.
// The response is parsed from JSON after the HTTP POST to the signing server.
type RemoteSignResponse struct {
	Signature string `json:"signature"`
	Error     string `json:"error,omitempty"`
}

// SignResult holds the result of a signing operation, including the signature
// and any metadata about the signing process.
type SignResult struct {
	Signature string `json:"signature"`
	Source    string `json:"source"`
	FilePath  string `json:"file_path"`
}
