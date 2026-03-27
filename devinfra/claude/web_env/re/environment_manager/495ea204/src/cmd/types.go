// Reconstructed from binary: environment-manager (Build ID 495ea204)
// Source: cmd/types.go (inferred - types used across cmd package)
// Package: github.com/anthropics/anthropic/api-go/environment-manager/cmd
//
// These types were discovered via type equality functions in the binary:
//   type:.eq.cmd.MCPRequest    (0xb7c920)
//   type:.eq.cmd.MCPError      (0xb7ca20)
//   type:.eq.cmd.MCPContent    (0xb7ca80)
//   type:.eq.cmd.CodeSignConfig (0xb7cb00)
//   type:.eq.cmd.TaskContext   (0xb7cb60)

package cmd

// MCPRequest represents an MCP (Model Context Protocol) JSON-RPC request.
//
// Binary type descriptor at 0xd76ba0 (size=64, ptrdata=64).
// Type equality: 0xb7c920 - type:.eq.cmd.MCPRequest
// Three string fields (JSONRPC, Method, ID) at offsets 0x00, 0x10, 0x20.
type MCPRequest struct {
	JSONRPC string `json:"jsonrpc"`
	Method  string `json:"method"`
	ID      string `json:"id,omitempty"`
}

// MCPToolCallParams holds the parameters for a tools/call MCP request.
//
// Discovered from RTTI at 0x80c1a5: ToolName json:"tool_name", Arguments json:"arguments"
type MCPToolCallParams struct {
	ToolName  string                 `json:"tool_name"`
	Arguments map[string]interface{} `json:"arguments"`
}

// MCPRequestWithParams combines MCPRequest with typed tool call parameters.
// This is the full request body sent to the MCP server.
type MCPRequestWithParams struct {
	JSONRPC string             `json:"jsonrpc"`
	Method  string             `json:"method"`
	ID      string             `json:"id,omitempty"`
	Params  *MCPToolCallParams `json:"params"`
}

// MCPError represents an MCP JSON-RPC error response.
//
// Type equality: 0xb7ca20 - type:.eq.cmd.MCPError
//
// Field layout (from equality function):
//
//	Offset 0x00: int64 - compared directly (CMPQ)
//	Offset 0x08: string (data ptr) - compared via runtime.memequal
//	Offset 0x10: string (length) - compared directly (CMPQ)
type MCPError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

// MCPContent represents an MCP content block in a response.
//
// Type equality: 0xb7ca80 - type:.eq.cmd.MCPContent
//
// Field layout (from equality function):
//
//	Offset 0x00: string (data ptr) - compared via runtime.memequal
//	Offset 0x08: string (length) - compared directly (CMPQ)
//	Offset 0x10: string (data ptr) - compared via runtime.memequal
//	Offset 0x18: string (length) - compared directly (CMPQ)
type MCPContent struct {
	Type string `json:"type"`
	Text string `json:"text"`
}

// MCPResponse represents a full MCP JSON-RPC response.
//
// Binary type descriptor at 0xd76c60 (size=64, ptrdata=64).
// RTTI fields: Content json:"content", IsError json:"isError", Error.
// Layout: Content(0x20, slice) + Error(0x38, ptr).
type MCPResponse struct {
	Content []MCPContent `json:"content"`
	IsError bool         `json:"isError"`
	Error   *MCPError    `json:"error"`
}

// MCPToolResult is the result portion of an MCP tools/call response.
//
// Binary type descriptor at 0xd33180 (size=32, ptrdata=8).
// Layout: Content(0x00, slice of MCPContent) + IsError(0x18, bool).
type MCPToolResult struct {
	Content []MCPContent `json:"content"`
	IsError bool         `json:"isError"`
}

// CodeSignConfig holds the configuration for code signing operations.
//
// Type equality: 0xb7cb00 - type:.eq.cmd.CodeSignConfig
//
// Field layout (from equality function):
//
//	Offset 0x00: int64 - compared directly (CMPQ)
//	Offset 0x08: string (data ptr) - compared via runtime.memequal
//	Offset 0x10: string (length) - compared directly (CMPQ)
type CodeSignConfig struct {
	Port  int    // offset 0x00 - MCP server port (from CODESIGN_MCP_PORT)
	Token string // offset 0x08 - MCP authentication token (from CODESIGN_MCP_TOKEN)
}

// TaskContext holds the context for a task execution, parsed from stdin
// or constructed from command-line flags.
//
// Type equality: 0xb7cb60 - type:.eq.cmd.TaskContext
//
// Field layout (from equality function - 10 field comparisons):
//
//	Offset 0x00: int64  - compared directly (CMPQ)
//	Offset 0x08: int64  - compared directly (CMPQ)
//	Offset 0x10: int64  - compared directly (CMPQ)
//	Offset 0x18: int64  - compared directly (CMPQ)
//	Offset 0x20: string (data ptr) - compared via runtime.memequal
//	Offset 0x28: string (length) - compared directly (CMPQ)
//	Offset 0x30: string (data ptr) - compared via runtime.memequal
//	Offset 0x38: string (length) - compared directly (CMPQ)
//	Offset 0x40: string (data ptr) - compared via runtime.memequal
//	Offset 0x48: string (length) - compared directly (CMPQ)
//	Offset 0x50: bool   - compared via MOVZX + CMPB
//
// Total size: 0x58 (88 bytes) with 4 int fields, 3 string fields, 1 bool.
type TaskContext struct {
	Field0 int    // offset 0x00 - possibly version or format type
	Field1 int    // offset 0x08 - possibly session-related identifier
	Field2 int    // offset 0x10 - possibly work-related identifier
	Field3 int    // offset 0x18 - possibly timestamp or sequence number
	Field4 string // offset 0x20 - possibly API URL or session ID
	Field5 string // offset 0x30 - possibly script path or working directory
	Field6 string // offset 0x40 - possibly output file or secret path
	Field7 bool   // offset 0x50 - possibly debug flag or stdin mode
}
