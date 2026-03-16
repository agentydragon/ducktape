// Reconstructed from environment-manager binary (Build ID: a6f96673)
// Source: internal/tunnel/ws_handler.go
// Module: github.com/anthropics/anthropic/api-go/environment-manager

package tunnel

import (
	"fmt"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"net/textproto"
	"sync"

	"github.com/gorilla/websocket"

	tunnelpb "github.com/anthropics/anthropic/api-go/environment-manager/internal/tunnel/tunnelpb"
)

// wsConnection represents a single WebSocket tunnel connection.
type wsConnection struct {
	conn         *websocket.Conn
	connectionID string
}

// WSHandler manages WebSocket tunnel connections. Uses a sync.RWMutex
// for concurrent access to the connections map.
//
// Binary address: methods at 0xb67980, 0xb689e0, 0xb68f00, 0xb69ea0
type WSHandler struct {
	logger      *slog.Logger
	connections map[string]*wsConnection // offset 0x10
	mu          sync.RWMutex             // offset 0x18
}

// OpenTunnel opens a new WebSocket tunnel connection to the local server.
// It validates the port range, constructs the WebSocket URL with port and path,
// copies request headers using textproto.CanonicalMIMEHeaderKey, establishes
// the WebSocket connection, and starts a readLoop goroutine for the upstream connection.
//
// Logs with 6 key-value pairs: path, port, url, connection_id, tunnel_id, headers.
//
// Binary address: 0xb67980
func (h *WSHandler) OpenTunnel(
	ctx interface{},
	wsOpen *tunnelpb.TunnelRequest_WsOpen,
	sender WSResponseSender,
	req *tunnelpb.TunnelRequest,
) {
	// Extract request fields
	var path string
	var port int32
	var url string

	if wsOpen != nil {
		path = wsOpen.GetPath()
		port = wsOpen.GetPort()
		url = wsOpen.GetUrl()
	}

	slog.Info("opening ws tunnel",
		"path", path,
		"port", port,
		"url", url,
		"connection_id", "",
		"tunnel_id", "",
		"headers", "",
	)

	// Validate port range (port-1 <= 0xFFFE means port in [1, 65535])
	if port-1 > 0xFFFE {
		slog.Error("invalid port for ws tunnel", "port", port)
		return
	}

	// Construct WebSocket URL: ws://localhost:{port}{path}
	wsURL := fmt.Sprintf("ws://localhost:%d%s", port, url)

	// Build HTTP headers from request
	headers := make(http.Header)

	// Generate a random connection ID
	connID := rand.Uint64()
	_ = connID

	// Copy headers from request using canonical MIME header keys
	if wsOpen != nil {
		for key, values := range wsOpen.GetHeaders() {
			canonKey := textproto.CanonicalMIMEHeaderKey(key)
			for _, v := range values {
				headers.Add(canonKey, v)
			}
		}
	}

	// Dial the local WebSocket server
	dialer := websocket.DefaultDialer
	conn, _, err := dialer.Dial(wsURL, headers)
	if err != nil {
		slog.Error("failed to open ws tunnel",
			"error", err,
			"url", wsURL,
		)
		return
	}

	// Store the connection
	h.mu.Lock()
	connectionID := fmt.Sprintf("%d", connID)
	h.connections[connectionID] = &wsConnection{
		conn:         conn,
		connectionID: connectionID,
	}
	h.mu.Unlock()

	// Send opened response
	sender.SendOpened(nil)

	// Start read loop in a goroutine
	go h.readLoop(ctx, sender, conn, connectionID)
}

// readLoop reads messages from an upstream WebSocket connection and sends
// them back through the tunnel. Runs in a goroutine started by OpenTunnel.
// On connection close, sends a WsClose response.
//
// Binary address: 0xb69820
func (h *WSHandler) readLoop(
	ctx interface{},
	sender WSResponseSender,
	conn *websocket.Conn,
	connectionID string,
) {
	defer func() {
		// Cleanup on exit - send close
		sender.SendClose(nil)
	}()

	// Start a goroutine for context cancellation
	go func() {
		// Monitor for context done and close connection
	}()

	for {
		msgType, message, err := conn.ReadMessage()
		if err != nil {
			// Handle close error
			closeErr, ok := err.(*websocket.CloseError)
			if ok {
				slog.Info("ws tunnel closed by upstream",
					"code", closeErr.Code,
					"reason", closeErr.Text,
					"connection_id", connectionID,
				)
			} else {
				slog.Warn("ws tunnel read error",
					"error", err,
					"connection_id", connectionID,
				)
			}
			return
		}

		// Create response message
		wsMsg := &tunnelpb.WsMessage{
			Type: int32(msgType),
			Data: message,
		}

		msgResp := &tunnelpb.TunnelResponse{
			Payload: &tunnelpb.TunnelResponse_WsMessage{
				WsMessage: wsMsg,
			},
		}
		if err := sender.SendMessage(msgResp); err != nil {
			slog.Warn("failed to send ws message through tunnel",
				"error", err,
				"connection_id", connectionID,
			)
			return
		}
	}
}

// SendMessage forwards a WebSocket message from the tunnel to the upstream
// WebSocket connection. Acquires a read lock on the connections map to find
// the connection, then writes the message.
//
// Binary address: 0xb689e0
func (h *WSHandler) SendMessage(
	wsMsg *tunnelpb.TunnelRequest_WsMessage,
	req *tunnelpb.TunnelRequest,
) {
	// Acquire read lock
	h.mu.RLock()

	// Look up connection by ID
	var connectionID string
	if wsMsg != nil {
		connectionID = wsMsg.GetConnectionId()
	}

	wsConn, ok := h.connections[connectionID]
	h.mu.RUnlock()

	if !ok {
		slog.Warn("ws tunnel not found for message",
			"connection_id", connectionID,
		)
		return
	}

	// Write the message to the upstream WebSocket
	msgType := websocket.TextMessage
	if wsMsg != nil {
		msgType = int(wsMsg.GetType())
	}

	data := wsMsg.GetData()
	if err := wsConn.conn.WriteMessage(msgType, data); err != nil {
		slog.Warn("failed to write ws message to upstream",
			"error", err,
			"connection_id", connectionID,
		)
	}
}

// CloseTunnel closes a specific WebSocket tunnel identified by connection ID.
// Logs the close with: path, port, connection_id, url fields.
//
// Binary address: 0xb68f00
func (h *WSHandler) CloseTunnel(wsClose *tunnelpb.TunnelRequest_WsClose) error {
	var path string
	var port int32
	var url string
	var connectionID string

	if wsClose != nil {
		path = wsClose.GetPath()
		port = wsClose.GetPort()
		url = wsClose.GetUrl()
		connectionID = wsClose.GetConnectionId()
	}

	slog.Info("closing ws tunnel",
		"path", path,
		"port", port,
		"connection_id", connectionID,
		"url", url,
	)

	return h.closeTunnelInstance(connectionID)
}

// closeTunnelInstance removes a connection from the map and closes it.
//
// Binary address: 0xb69140
func (h *WSHandler) closeTunnelInstance(connectionID string) error {
	h.mu.Lock()
	wsConn, ok := h.connections[connectionID]
	if ok {
		delete(h.connections, connectionID)
	}
	h.mu.Unlock()

	if !ok {
		return fmt.Errorf("ws tunnel not found: %s", connectionID)
	}

	return wsConn.conn.Close()
}

// closeTunnelWithCode closes a WebSocket connection with a specific close code.
// Used by CloseAll to gracefully close all connections.
//
// Binary address: 0xb693c0
func (h *WSHandler) closeTunnelWithCode(connectionID string, code int, reason string) {
	h.mu.Lock()
	wsConn, ok := h.connections[connectionID]
	if ok {
		delete(h.connections, connectionID)
	}
	h.mu.Unlock()

	if !ok {
		return
	}

	// Send close message with code
	msg := websocket.FormatCloseMessage(code, reason)
	wsConn.conn.WriteMessage(websocket.CloseMessage, msg)
	wsConn.conn.Close()
}

// CloseAll closes all WebSocket tunnel connections. Acquires the write lock,
// collects all connections into a slice, releases the lock, then closes each
// connection with close code 1000 (normal closure).
//
// Binary address: 0xb69ea0
func (h *WSHandler) CloseAll() {
	h.mu.Lock()

	// Get the number of connections
	count := 0
	if h.connections != nil {
		count = len(h.connections)
	}

	// Collect all connections into a slice
	type connEntry struct {
		id   string
		conn *wsConnection
	}
	var entries []connEntry

	if count <= 2 {
		// Small count: use stack-allocated array
		entries = make([]connEntry, 0, 2)
	} else {
		entries = make([]connEntry, 0, count)
	}

	for id, conn := range h.connections {
		entries = append(entries, connEntry{id: id, conn: conn})
	}

	h.mu.Unlock()

	// Close each connection with code 1000 (normal closure)
	for _, entry := range entries {
		h.closeTunnelWithCode(entry.id, 1000, "") // 0x3e8 = 1000
	}
}
