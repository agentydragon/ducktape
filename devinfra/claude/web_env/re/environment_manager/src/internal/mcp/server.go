// server.go contains the BaseServer implementation for MCP servers,
// providing common HTTP server lifecycle, bearer auth and tool registration.
//
// Originally reconstructed from a6f96673 DWARF extraction, carried forward to
// 495ea204, then re-anchored against build `release-1186d93b9-ext`
// (Build ID 0b86a2a0dbc9411eb18435e1c56822b0156f90fe).
// Source path: /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/mcp/server.go
//
// Garbled identities in the new binary (package `uBHoqupaaEIs`):
//
//	BaseServer      -> `Lw9QeTzj`
//	responseWriter  -> `atnXts3i`
//
// Key symbols, new binary (old binary in parentheses):
//
//	mcp.(*BaseServer).GetName                   0x1770760  (0x15b2440)
//	mcp.(*BaseServer).GetTools                  0x1770780  (0x15b2460)
//	mcp.(*BaseServer).Start                     0x17707a0  (0x15b2480)
//	mcp.(*BaseServer).Stop                      0x17782a0  (0x15bc500)
//	mcp.(*BaseServer).ShouldRegisterWithClaude  0x17785c0  (0x15bc9c0)
//	mcp.(*BaseServer).GetConfig                 0x17785e0  (absent in old)
//	mcp.(*responseWriter).WriteHeader           0x17705c0
//	mcp.(*responseWriter).Write                 0x1770620
//	mcp.responseWriter.Header                   0x1783cc0
//	mcp.(*responseWriter).Header                0x1783d40
//
// Vendored mcp-go identities referenced from here (v0.54.1, package `nwTZT_5`):
//
//	server.NewMCPServer               -> nwTZT_5.EWfyUsXX2a
//	server.NewStreamableHTTPServer    -> nwTZT_5.BTX3DI
//	server.MCPServer                  -> nwTZT_5.MAKP4pBZG
//	server.StreamableHTTPServer       -> nwTZT_5.ImIohM4fkR
//	(*MCPServer).AddTools             -> nwTZT_5.(*MAKP4pBZG).AddTools
//	(*StreamableHTTPServer).ServeHTTP -> nwTZT_5.(*ImIohM4fkR).ServeHTTP
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

// ServerConfig holds the static configuration for creating a BaseServer.
//
// Carried from the a6f96673 DWARF extraction (type:.eq at 0xad2740).
//
// TODO(re): not re-derived against the 0b86a2a0 binary. Not to be confused with
// Config in registry.go, which is the *runtime* descriptor GetConfig returns.
type ServerConfig struct {
	Name    string
	Version string
}

// BaseServer provides the common HTTP server infrastructure for MCP servers.
// Concrete MCP server implementations embed BaseServer to inherit lifecycle
// management (Start, Stop, GetConfig) and override GetTools /
// ShouldRegisterWithClaude.
//
// Struct layout, from the field accesses in Start/Stop/GetConfig:
//
//	offset 0x00: logger *slog.Logger
//	offset 0x08: name string                (ptr at 0x08, len at 0x10)
//	offset 0x18: version string             (ptr at 0x18, len at 0x20)
//	offset 0x28: httpServer *http.Server
//	offset 0x30: streamableServer *mcpserver.StreamableHTTPServer
//	offset 0x38: listener net.Listener
//	offset 0x40: listenerAddr net.Addr
//	offset 0x48: bearerToken string         (ptr at 0x48, len at 0x50)
//	offset 0x58: port int
//	offset 0x60: stopCh chan struct{}
//
// The 0x08/0x10 (name) and 0x48/0x50/0x58 (bearerToken, port) offsets are
// re-confirmed on the new binary by GetName (0x1770760) and GetConfig
// (0x17785e0). The remainder is carried from a6f96673.
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
// Binary: 0x1770760
//
//	MOVQ 0x8(AX), CX   ; name.ptr
//	MOVQ 0x10(AX), BX  ; name.len
//	MOVQ CX, AX
//	RET
func (s *BaseServer) GetName() string {
	return s.name
}

// GetTools returns an empty tool list. Concrete implementations override this.
//
// Binary: 0x1770780
//
//	LEAQ <runtime.zerobase>, AX
//	XORL BX, BX
//	MOVQ BX, CX
//	RET
func (s *BaseServer) GetTools() []ToolConfig {
	return nil
}

// ShouldRegisterWithClaude returns true by default. Concrete implementations
// can override this to control whether they appear in the generated Claude Code
// MCP configuration.
//
// Binary: 0x17785c0 — MOVL $0x1, AX; RET
func (s *BaseServer) ShouldRegisterWithClaude() bool {
	return true
}

// GetConfig returns the started server's port and bearer token so the registry
// can emit the Claude Code MCP configuration entry for it.
//
// Binary: 0x17785e0 — NEW in this build (no counterpart in the old binary).
//
// Assembly:
//
//	MOVQ AX, 0x20(SP)                       ; receiver
//	LEAQ <type Config>, AX
//	CALL runtime.newobject                  ; 0x18-byte heap object
//	MOVQ 0x20(SP), CX                       ; receiver
//	MOVQ 0x58(CX), DX ; MOVQ DX, 0(AX)      ; Config.Port = s.port
//	MOVQ 0x50(CX), DX
//	MOVQ 0x48(CX), CX
//	MOVQ DX, 0x10(AX)                       ; Config.BearerToken.len
//	CALL runtime.gcWriteBarrier1
//	MOVQ CX, 0x8(AX)                        ; Config.BearerToken.ptr
//	RET
func (s *BaseServer) GetConfig() *Config {
	return &Config{
		Port:        s.port,
		BearerToken: s.bearerToken,
	}
}

// Start initializes and starts the MCP HTTP server.
//
// Binary: 0x17707a0 (old: 0x15b2480; a6f96673: 0xacfe80)
//
// Ordered outbound calls recovered from the disassembly:
//
//	nwTZT_5.EWfyUsXX2a                  = mcpserver.NewMCPServer
//	nwTZT_5.(*MAKP4pBZG).AddTools       = (*mcpserver.MCPServer).AddTools
//	nwTZT_5.BTX3DI                      = mcpserver.NewStreamableHTTPServer
//	Start.func1 (0x177e240)             = StreamableHTTPOption closure
//	Start.func2 (0x177e760)             = StreamableHTTPOption closure
//	mWIfHHslo.RgX6eVa                   = crypto/rand.Read
//	aHae1zHtl.(*JI1WMF).EncodeToString  = base64.Encoding.EncodeToString
//	Start.func6 (0x177ea80)
//	x3ZgH1.IsCCpnypzEn                  = net.Listen
//	bmbxQn.AEjJ1Z__rd                   = fmt.Sprintf
//	Start.func13 (0x17838c0)
//
// String xrefs of Start are ["0", "tcp"] — net.Listen("tcp", …:0), i.e. an
// ephemeral port, same as the old binary.
//
// The HTTP handler is Start.func10 (0x1773d00): it references the literal
// "Bearer ", reads a request header (`zFiF3gO.Wazd7Z.Get` = http.Header.Get),
// and on success forwards to `nwTZT_5.(*ImIohM4fkR).ServeHTTP` — bearer-token
// auth middleware wrapped around mcp-go's StreamableHTTPServer. The old binary
// has the same shape at `a8IEAlutXX1f.(*AkqABij02).Start.func10` (0x15b6be0),
// so the auth model is unchanged.
//
// TODO(re): the option closures' payloads (endpoint path, stateless flag) are
// garble-encrypted literals and were not recovered on this build; the option
// set below is carried from the a6f96673 DWARF RE.
func (s *BaseServer) Start(logger *slog.Logger, name string) (int, error) {
	s.logger = logger

	// Create MCP server (mcp-go v0.54.1).
	mcpSrv := mcpserver.NewMCPServer(s.name, s.version)

	// Register tools with the MCP server.
	toolConfigs := s.GetTools()
	if len(toolConfigs) > 0 {
		serverTools := make([]mcpserver.ServerTool, len(toolConfigs))
		for i, tc := range toolConfigs {
			serverTools[i] = mcpserver.ServerTool{
				Tool:    tc.Tool,
				Handler: tc.Handler,
			}
		}
		mcpSrv.AddTools(serverTools...)
	}

	// Create streamable HTTP server with options.
	opts := []mcpserver.StreamableHTTPOption{
		// TODO(re): Start.func1 (0x177e240) and Start.func2 (0x177e760) —
		// option payloads not recovered. a6f96673 had WithEndpointPath("/mcp")
		// and WithStateLess(true).
	}
	streamable := mcpserver.NewStreamableHTTPServer(mcpSrv, opts...)
	s.streamableServer = streamable

	// Generate bearer token.
	if debugHardcodedToken {
		logger.Warn(
			"Using hardcoded bearer token for debugging", // TODO(re): literal encrypted on this build.
			"server", s.name,
			"token", s.bearerToken,
		)
	} else {
		buf := make([]byte, 32)
		if _, err := rand.Read(buf); err != nil {
			return 0, fmt.Errorf("failed to read random data: %w", err)
		}
		s.bearerToken = base64.URLEncoding.EncodeToString(buf)
	}

	listener, err := net.Listen("tcp", ":0")
	if err != nil {
		return 0, err
	}
	s.listener = listener
	s.listenerAddr = listener.Addr()

	if tcpAddr, ok := listener.Addr().(*net.TCPAddr); ok {
		s.port = tcpAddr.Port
	}

	// Bearer-auth middleware in front of the streamable HTTP server.
	// Binary: Start.func10 at 0x1773d00.
	s.httpServer = &http.Server{
		Handler: http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			// TODO(re): the header name and the rejection status/body are
			// garble-encrypted; the binary calls net/http's error helper
			// (q9Y582tbRs.XE9fsVxbmba_) on the failure path.
			if r.Header.Get("Authorization") != "Bearer "+s.bearerToken {
				http.Error(w, "", http.StatusUnauthorized)
				return
			}
			streamable.ServeHTTP(w, r)
		}),
	}

	// Serve + heartbeat goroutines (two runtime.newproc call sites in Start).
	s.stopCh = make(chan struct{})
	go func() {
		if err := s.httpServer.Serve(listener); err != nil && err != http.ErrServerClosed {
			logger.Error("MCP HTTP server error", "error", err, "server", s.name) // TODO(re): literal encrypted.
		}
	}()

	// TODO(re): heartbeat interval not recovered on this build; 30s is carried
	// from the a6f96673 RE (see heartbeatDuration in registry.go).
	go func() {
		ticker := time.NewTicker(30 * time.Second)
		defer ticker.Stop()

		for {
			select {
			case <-s.stopCh:
				return
			case <-ticker.C:
				logger.Debug("MCP server heartbeat", "server", s.name, "port", s.port) // TODO(re): literal encrypted.
			}
		}
	}()

	return s.port, nil
}

// Stop gracefully shuts down the MCP HTTP server.
//
// Binary: 0x17782a0 (old: 0x15bc500; a6f96673: 0xad2320)
//
// Outbound calls: `s3Vyd2awFy.AKSPaFQWx6C` (context.WithTimeout),
// `q9Y582tbRs.(*SMyN0d).Shutdown` (http.Server.Shutdown), slog, plus two
// closures Stop.func1 (0x177d400) and Stop.func2 (0x177dec0). The old binary's
// Stop had only Stop.func2 (0x15c2b00), so one shutdown step was added here.
func (s *BaseServer) Stop() bool {
	stopped := false

	if s.httpServer != nil {
		ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()

		if err := s.httpServer.Shutdown(ctx); err != nil {
			s.logger.Warn("error shutting down MCP server", "error", err, "server", s.name) // TODO(re): literal encrypted.
		}
		stopped = true
	}

	// mcp-go v0.54.1's StreamableHTTPServer exposes Shutdown(ctx)
	// (nwTZT_5.(*ImIohM4fkR).Shutdown).
	// TODO(re): confirm Stop.func1 (0x177d400) is that shutdown — the callee is
	// reached through a closure and was not resolved.
	if s.streamableServer != nil {
		shutdownCtx, shutdownCancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer shutdownCancel()
		s.streamableServer.Shutdown(shutdownCtx)
	}

	if s.stopCh != nil {
		close(s.stopCh)
	}

	return stopped
}

// responseWriter is the http.ResponseWriter wrapper used by the bearer-auth
// middleware so the MCP response can be buffered/inspected before it reaches
// the real writer.
//
// Garbled type: `uBHoqupaaEIs.atnXts3i`. It satisfies http.ResponseWriter —
// garble had to keep Header/Write/WriteHeader in the clear for the interface
// check to hold, which is how the type was identified.
//
// Struct layout carried from a6f96673 (type:.eq at 0xad25e0):
//
//	offset 0x00: header http.Header
//	offset 0x08: body []byte
//	offset 0x20: statusCode int
type responseWriter struct {
	header     http.Header
	body       []byte
	statusCode int
}

// Header returns the response headers.
// Binary: 0x1783d40 (pointer receiver), 0x1783cc0 (value receiver).
func (w *responseWriter) Header() http.Header {
	return w.header
}

// Write appends data to the response body buffer.
// Binary: 0x1770620 (closure Write.func1 at 0x1778660).
func (w *responseWriter) Write(data []byte) (int, error) {
	w.body = append(w.body, data...)
	return len(data), nil
}

// WriteHeader records the HTTP status code.
// Binary: 0x17705c0
func (w *responseWriter) WriteHeader(statusCode int) {
	w.statusCode = statusCode
}
