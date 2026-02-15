// server.go contains the BaseServer implementation for MCP servers,
// providing common HTTP server lifecycle and tool registration.
//
// Reconstructed from binary at Build ID 6b49f1ca (Go 1.25.6).
// Source path: /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/mcp/server.go
//
// Key symbols:
//   - mcp.(*BaseServer).GetName (0xacfe40)
//   - mcp.(*BaseServer).GetTools (0xacfe60)
//   - mcp.(*BaseServer).Start (0xacfe80)
//   - mcp.(*BaseServer).Stop (0xad2320)
//   - mcp.(*BaseServer).ShouldRegisterWithClaude (0xad25c0)
//   - mcp.(*responseWriter) methods (0xacfcc0, 0xacfd20, 0xad26e0)
package mcp

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"time"

	mcpserver "github.com/mark3labs/mcp-go/server"
)

// ServerConfig holds the configuration for creating a BaseServer.
//
// type:.eq at 0xad2740 confirms this is a comparable struct.
type ServerConfig struct {
	Name    string
	Version string
}

// BaseServer provides the common HTTP server infrastructure for MCP servers.
// Concrete MCP server implementations embed BaseServer to inherit lifecycle
// management (Start, Stop) and override GetTools/ShouldRegisterWithClaude.
//
// Struct layout (from field access patterns in Start/Stop):
//   offset 0x00: logger *slog.Logger        (accessed at 0xacfeb5, 0xad23b6)
//   offset 0x08: name string                (ptr at 0x08, len at 0x10)
//   offset 0x18: version string             (ptr at 0x18, len at 0x20)
//   offset 0x28: httpServer *http.Server    (checked at 0xad2351, shutdown at 0xad23a0)
//   offset 0x30: streamableServer *mcpserver.StreamableHTTPServer
//   offset 0x38: listener net.Listener      (set at 0xad0376)
//   offset 0x40: listenerAddr net.Addr      (set at 0xad03a0)
//   offset 0x48: bearerToken string         (ptr at 0x48, len at 0x50)
//   offset 0x58: port int                   (set at 0xad03c9)
//   offset 0x60: stopCh chan struct{}        (closed at 0xad24c9)
//
// type:.eq at 0xad27a0 confirms comparability.
type BaseServer struct {
	logger           *slog.Logger
	name             string
	version          string
	httpServer       *http.Server
	streamableServer *mcpserver.StreamableHTTPServer
	listener         net.Listener
	listenerAddr     net.Addr
	bearerToken      string
	port             int
	stopCh           chan struct{}
}

// GetName returns the server's name string.
//
// Binary address: 0xacfe40
// Source file: server.go
//
// Assembly (trivial):
//   MOVQ 0x8(AX), CX   ; name.ptr
//   MOVQ 0x10(AX), BX   ; name.len
//   MOVQ CX, AX
//   RET
func (s *BaseServer) GetName() (string, int) {
	return s.name, len(s.name)
}

// GetTools returns an empty tool list. Concrete implementations override this.
//
// Binary address: 0xacfe60
// Source file: server.go
//
// Assembly (returns empty slice):
//   LEA <static empty slice>, AX
//   XOR BX, BX
//   MOV BX, CX
//   RET
func (s *BaseServer) GetTools() ([]mcpserver.ServerTool, int, int) {
	return nil, 0, 0
}

// ShouldRegisterWithClaude returns true by default. Concrete implementations
// can override this to control registration behavior.
//
// Binary address: 0xad25c0
// Source file: server.go
//
// Assembly (trivial):
//   MOVL $0x1, AX
//   RET
func (s *BaseServer) ShouldRegisterWithClaude() bool {
	return true
}

// Start initializes and starts the MCP HTTP server. It:
//   1. Creates an mcp-go MCPServer with the configured name/version
//   2. Adds tools from the provided tool definitions
//   3. Creates a StreamableHTTPServer with endpoint path and stateless options
//   4. Generates a bearer token (random or debug hardcoded)
//   5. Binds to a TCP listener on localhost
//   6. Logs server details (name, port, address)
//   7. Starts serving in a goroutine
//   8. Launches a heartbeat goroutine
//
// Binary address: 0xacfe80
// Source file: server.go
//
// Assembly evidence:
//   - mcpserver.NewMCPServer at 0xacfed0
//   - mcpserver.AddTools at 0xacffce
//   - mcpserver.NewStreamableHTTPServer with WithEndpointPath (func4 at 0xacffff)
//     and WithStateLess (func5 at 0xad002a)
//   - debugHardcodedToken check at 0xad00a5
//   - "Using hardcoded bearer token for debugging" log at 0xad018a
//   - crypto/rand.Read 32 bytes at 0xad01ad
//   - base64.URLEncoding.EncodeToString at 0xad0220
//   - net.Listen("tcp", ":0") at 0xad0360
//   - "MCP server started" log at 0xad0345 area
//   - "TCP listener created successfully" log at 0xad04d2
//   - goroutine for Serve (func1 at 0xad13e0)
//   - goroutine for heartbeat (func2 at 0xad0d00)
func (s *BaseServer) Start(logger *slog.Logger, name string) (int, error) {
	s.logger = logger

	// Create MCP server
	mcpSrv := mcpserver.NewMCPServer(s.name, s.version)

	// Register tools with the MCP server
	tools, _, _ := s.GetTools()
	if len(tools) > 0 {
		mcpSrv.AddTools(tools...)
	}

	// Create streamable HTTP server with options
	opts := []mcpserver.StreamableHTTPOption{
		// WithEndpointPath (func4 at 0xad0280): sets endpoint to "/mcp"
		// WithStateLess (func5 at 0xad002a): enables stateless mode
	}
	streamable := mcpserver.NewStreamableHTTPServer(mcpSrv, opts...)
	s.streamableServer = streamable

	// Generate bearer token
	if debugHardcodedToken {
		logger.Warn("Using hardcoded bearer token for debugging",
			"server", s.name,
			"token", s.bearerToken,
		)
	} else {
		buf := make([]byte, 32)
		if _, err := rand.Read(buf); err != nil {
			return 0, fmt.Errorf("failed to read random data: %w", err)
		}
		s.bearerToken = base64.URLEncoding.EncodeToString(buf)
		logger.Info("MCP server token generated",
			"server", s.name,
		)
	}

	// Listen on TCP
	addr := ":" + name
	listener, err := net.Listen("tcp", addr)
	if err != nil {
		return 0, err
	}
	s.listener = listener
	s.listenerAddr = listener.Addr()

	// Extract port from listener address
	if tcpAddr, ok := listener.Addr().(*net.TCPAddr); ok {
		s.port = tcpAddr.Port
	}

	logger.Info("TCP listener created successfully",
		"server", s.name,
		"address", listener.Addr().String(),
		"port", s.port,
	)

	// Create HTTP server with the streamable handler
	s.httpServer = &http.Server{
		Handler: streamable,
	}

	// Start serving in background goroutine (func1 at 0xad13e0)
	s.stopCh = make(chan struct{})
	go func() {
		if err := s.httpServer.Serve(listener); err != nil && err != http.ErrServerClosed {
			logger.Error("MCP HTTP server error",
				"error", err,
				"server", s.name,
			)
		}
	}()

	// Start heartbeat goroutine (func2 at 0xad0d00)
	go func() {
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()

		for {
			select {
			case <-s.stopCh:
				return
			case <-ticker.C:
				logger.Debug("MCP server heartbeat",
					"server", s.name,
					"port", s.port,
				)
			}
		}
	}()

	return s.port, nil
}

// Stop gracefully shuts down the MCP HTTP server. It:
//   1. If httpServer is non-nil, calls Shutdown with a 5-second timeout
//   2. On shutdown error, logs a warning
//   3. Calls the streamable server's cleanup if present
//   4. Closes the stop channel
//
// Binary address: 0xad2320
// Source file: server.go
//
// Assembly evidence:
//   - Check httpServer != nil at 0xad2351
//   - context.WithTimeout(background, 5*time.Second) at 0xad2377
//     (timeout value 0x12a05f200 = 5,000,000,000 ns = 5s)
//   - http.Server.Shutdown at 0xad23a0
//   - On error: log.Warn "error shutting down MCP server" at 0xad2487
//   - Close stopCh at 0xad24c9
func (s *BaseServer) Stop() bool {
	stopped := false

	if s.httpServer != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()

		if err := s.httpServer.Shutdown(ctx); err != nil {
			s.logger.Warn("error shutting down MCP server",
				"error", err,
				"server", s.name,
			)
		}
		stopped = true
	}

	// Call cleanup on streamable server if present
	if s.streamableServer != nil {
		// TODO(re): streamable server cleanup not reconstructed
	}

	// Close stop channel to signal goroutines
	if s.stopCh != nil {
		close(s.stopCh)
	}

	return stopped
}

// responseWriter is a custom http.ResponseWriter used for intercepting
// MCP HTTP responses (e.g., for bearer token validation).
//
// itab: *mcp.responseWriter → net/http.ResponseWriter (0xf63bc0)
//
// Struct layout (from type:.eq at 0xad25e0):
//   offset 0x00: header http.Header
//   offset 0x08: body []byte
//   offset 0x20: statusCode int
type responseWriter struct {
	header     http.Header
	body       []byte
	statusCode int
}

// Header returns the response headers.
// Binary address: 0xad26e0 (value receiver), 0xad2660 (pointer receiver called via interface)
func (w *responseWriter) Header() http.Header {
	return w.header
}

// Write appends data to the response body buffer.
// Binary address: 0xacfd20
func (w *responseWriter) Write(data []byte) (int, error) {
	w.body = append(w.body, data...)
	return len(data), nil
}

// WriteHeader records the HTTP status code.
// Binary address: 0xacfcc0
func (w *responseWriter) WriteHeader(statusCode int) {
	w.statusCode = statusCode
}
