// Reconstructed from environment-manager binary (Build ID: 64bc4dc1)
// Source: internal/tunnel/client.go
// Module: github.com/anthropics/anthropic/api-go/environment-manager

package tunnel

import (
	"context"
	"fmt"
	"log/slog"
	"math/rand/v2"
	"net/http"
	"time"

	"github.com/gorilla/websocket"
	"golang.org/x/sync/errgroup"
	"google.golang.org/protobuf/encoding/protojson"

	"github.com/anthropics/anthropic/api-go/environment-manager/internal/dogmetrics"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/manager"
	"github.com/anthropics/anthropic/api-go/environment-manager/internal/tunnel/actions"
	tunnelpb "github.com/anthropics/anthropic/api-go/environment-manager/internal/tunnel/tunnelpb"

	isync "sync"
)

// ResponseSender is the interface for sending HTTP tunnel responses back through the tunnel.
type ResponseSender interface {
	SendHeaders(resp *tunnelpb.TunnelResponse) error
	SendChunk(resp *tunnelpb.TunnelResponse) error
	SendError(resp *tunnelpb.TunnelResponse) error
}

// WSResponseSender is the interface for sending WebSocket tunnel responses back through the tunnel.
type WSResponseSender interface {
	SendOpened(resp *tunnelpb.TunnelResponse) error
	SendMessage(resp *tunnelpb.TunnelResponse) error
	SendClose(resp *tunnelpb.TunnelResponse) error
	SendError(resp *tunnelpb.TunnelResponse) error
}

// Client implements the tunnel client that connects to the tunnel server
// and dispatches incoming requests to local handlers.
// Implements manager.TunnelClient.
//
// Binary address: type at multiple locations; see individual methods.
type Client struct {
	logger        *slog.Logger       // offset 0x00
	metricsClient dogmetrics.Client  // offset 0x08 (interface: itab + data, 16 bytes)
	httpClient    *httpClientWrapper // offset 0x18
	wsHandler     *WSHandler         // offset 0x20
	tunnelID      string             // offset 0x28 (ptr + len)
	endpoint      string             // offset 0x38 (ptr + len)
	authToken     string             // offset 0x48 (ptr + len)
	registry      *actions.Registry  // offset 0x58
	conn          *websocket.Conn    // offset 0x60
	mu            isync.Mutex        // offset 0x68
	// ... additional internal fields
	handler      *Handler
	writeTimeout time.Duration // offset 0x110, default 30s (0x6fc23ac00)
}

type httpClientWrapper struct {
	baseURL    string
	httpClient interface{} // *http.Client with custom Transport
	requests   map[string]interface{}
}

// httpResponseSender wraps the client to send HTTP responses through the tunnel.
//
// Binary address: methods at 0xb656a0, 0xb65760, 0xb65820
type httpResponseSender struct {
	client    *Client
	requestID string
	bodyLen   int
	logger    *slog.Logger
}

// wsResponseSender wraps the client to send WebSocket responses through the tunnel.
//
// Binary address: methods at 0xb658e0, 0xb659a0, 0xb65a60, 0xb65b20
type wsResponseSender struct {
	client       *Client
	connectionID string
	tunnelID     uint64
}

// NewClient creates a new tunnel Client with the given configuration.
//
// Binary address: 0xb60520
func NewClient(
	logger *slog.Logger,
	metricsClient dogmetrics.Client,
	tunnelID string,
	endpoint string,
	authToken string,
	registry *actions.Registry,
	opts ...func(*Client),
) *Client {
	c := &Client{
		writeTimeout: 30 * time.Second, // 0x6fc23ac00 ns
	}

	// Binary: 0xb60592-0xb60628 - allocates httpClientWrapper, creates http.Client
	// with *net/http.Transport as RoundTripper (itab at 0xb605dc)
	httpWrapper := &httpClientWrapper{
		baseURL: endpoint,
	}
	httpWrapper.httpClient = newHTTPClientWithTransport()

	reqMap := make(map[string]interface{})

	handler := &Handler{
		baseURL:  endpoint,
		requests: reqMap,
	}

	wsHandler := newWSHandler(logger, httpWrapper, handler)

	c.logger = logger
	c.metricsClient = metricsClient
	c.httpClient = httpWrapper
	c.wsHandler = wsHandler
	c.tunnelID = tunnelID
	c.endpoint = endpoint
	c.authToken = authToken
	c.registry = registry
	c.handler = handler

	for _, opt := range opts {
		opt(c)
	}

	return c
}

// newHTTPClientWithTransport creates an *http.Client with a standard
// net/http.Transport as the RoundTripper.
//
// Binary: inlined into NewClient at 0xb605d0-0xb60628
// Evidence: go:itab.*net/http.Transport,net/http.RoundTripper at 0xb605dc
func newHTTPClientWithTransport() interface{} {
	return &http.Client{
		Transport: &http.Transport{},
	}
}

// newWSHandler creates a WSHandler for handling WebSocket tunnel connections.
//
// Binary: inlined into NewClient at 0xb60696-0xb6075c
func newWSHandler(logger *slog.Logger, wrapper *httpClientWrapper, handler *Handler) *WSHandler {
	return &WSHandler{
		logger:      logger,
		connections: make(map[string]*wsConnection),
	}
}

// Connect establishes a WebSocket connection to the tunnel server.
// It acquires the client mutex, constructs the tunnel URL with auth parameters,
// sets up HTTP headers, and dials the WebSocket endpoint.
//
// Binary address: 0xb60800
func (c *Client) Connect(ctx context.Context) error {
	c.mu.Lock()
	defer c.mu.Unlock()

	// Trim trailing "/" from endpoint if present
	endpoint := c.endpoint
	if len(endpoint) > 0 && endpoint[len(endpoint)-1] == '/' {
		endpoint = endpoint[:len(endpoint)-1]
	}

	// Construct tunnel URL with query parameters
	url := fmt.Sprintf("%s?tunnel_id=%s&session_id=%s&environment_id=%s", endpoint, c.tunnelID, c.tunnelID, c.tunnelID)

	slog.Info("connecting to tunnel",
		"url", url,
		"tunnel_id", c.tunnelID,
	)

	// Set up request headers
	headers := make(map[string][]string)
	if c.authToken != "" {
		headers[textprotoCanonicalMIMEHeaderKey("Authorization")] = []string{c.authToken}
	}
	headers[textprotoCanonicalMIMEHeaderKey("X-Tunnel-Id")] = []string{c.tunnelID}

	// Create WebSocket dialer with timeout
	dialer := &websocket.Dialer{
		HandshakeTimeout: 10 * time.Second, // 0x2540be400 ns
	}

	// Dial the tunnel server
	conn, resp, err := dialer.DialContext(ctx, url, headers)
	if resp != nil && resp.Body != nil {
		defer resp.Body.Close()
	}

	if err != nil {
		if conn != nil {
			return fmt.Errorf("tunnel connection failed with status %d: %w", resp.StatusCode, err)
		}
		return fmt.Errorf("failed to connect to tunnel server: %w", err)
	}

	// Store connection and set pong handler
	c.conn = conn
	conn.SetPongHandler(func(string) error {
		return nil
	})

	slog.Info("connected to tunnel",
		"url", url,
		"tunnel_id", c.tunnelID,
	)

	return nil
}

// Run starts the main event loop for the tunnel client. It creates an errgroup
// that runs readLoop and pingLoop concurrently. When either loop exits,
// the other is cancelled.
//
// Binary address: 0xb61280
func (c *Client) Run(ctx context.Context) error {
	c.mu.Lock()
	conn := c.conn
	c.mu.Unlock()

	if conn == nil {
		return fmt.Errorf("tunnel client not connected")
	}

	ctx, cancel := context.WithCancelCause(ctx)
	defer cancel(nil)

	g := new(errgroup.Group)

	g.Go(func() error {
		return c.readLoop(ctx, cancel, conn)
	})

	g.Go(func() error {
		return c.pingLoop(ctx, cancel, conn)
	})

	g.Go(func() error {
		<-ctx.Done()
		c.closeConnection()
		return nil
	})

	return g.Wait()
}

// readLoop continuously reads messages from the WebSocket connection,
// unmarshals them as TunnelRequest protobuf messages, and dispatches
// them to the appropriate handler based on the request type.
//
// Binary address: 0xb61680
func (c *Client) readLoop(ctx context.Context, cancel context.CancelCauseFunc, conn *websocket.Conn) error {
	defer func() {
		// cleanup on exit
	}()

	for {
		// Check if context is done (non-blocking)
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}

		// Set read deadline (60 seconds)
		deadline := time.Now().Add(60 * time.Second) // 0xdf8475800 ns
		conn.SetReadDeadline(deadline)

		// Read next message
		_, message, err := conn.ReadMessage()
		if err != nil {
			// Handle read error - check if it's a close error, etc.
			return err
		}

		// Unmarshal the protobuf request
		req := &tunnelpb.TunnelRequest{}
		if err := protojson.Unmarshal(message, req); err != nil {
			// Log unmarshal error and continue
			continue
		}

		// Dispatch based on request type
		switch payload := req.GetPayload().(type) {
		case *tunnelpb.TunnelRequest_HttpRequest:
			go func() {
				defer func() {
					if r := recover(); r != nil {
						slog.Error("panic in handleHTTPRequest", "error", r)
					}
				}()
				c.handleHTTPRequest(ctx, payload, req)
			}()

		case *tunnelpb.TunnelRequest_HttpCancel:
			c.handleHTTPCancel(ctx, payload)

		case *tunnelpb.TunnelRequest_WsOpen:
			go func() {
				defer func() {
					if r := recover(); r != nil {
						slog.Error("panic in handleWSOpen", "error", r)
					}
				}()
				c.handleWSOpen(ctx, payload, req)
			}()

		case *tunnelpb.TunnelRequest_WsMessage:
			c.handleWSMessage(ctx, payload, req)

		case *tunnelpb.TunnelRequest_WsClose:
			c.handleWSClose(ctx, payload, req)
		}
	}
}

// handleHTTPRequest processes an incoming HTTP request from the tunnel.
// It creates a ResponseSender, checks if the path matches an action prefix
// ("/__actions/"), and either dispatches to the action registry or forwards
// to the local HTTP handler.
//
// Binary address: 0xb62540
func (c *Client) handleHTTPRequest(ctx context.Context, httpReq *tunnelpb.TunnelRequest_HttpRequest, req *tunnelpb.TunnelRequest) {
	// Create response sender
	sender := &httpResponseSender{
		client: c,
		logger: c.logger,
	}

	// Check if the request has an action path (starts with "/__actions/")
	if req.GetPath() != "" {
		path := req.GetPath()
		if len(path) >= 11 && path[:11] == "/__actions/" {
			// Dispatch to action registry
			// Convert repeated Header messages to map[string]string
			headers := make(map[string]string)
			for _, h := range req.GetHeaders() {
				headers[h.GetName()] = h.GetValue()
			}
			c.registry.Execute(
				ctx,
				req.GetBody(),
				headers,
				sender,
				path,
			)
			return
		}
	}

	// Forward to local HTTP handler
	c.handler.HandleRequest(ctx, httpReq.HttpRequest, sender)
}

// handleHTTPCancel handles a cancellation request for an in-flight HTTP request.
//
// Binary address: 0xb628e0
func (c *Client) handleHTTPCancel(ctx context.Context, cancel *tunnelpb.TunnelRequest_HttpCancel) {
	c.handler.CancelRequest(cancel.GetRequestId())
}

// handleWSOpen handles a WebSocket tunnel open request.
//
// Binary address: 0xb62a20
func (c *Client) handleWSOpen(ctx context.Context, wsOpen *tunnelpb.TunnelRequest_WsOpen, req *tunnelpb.TunnelRequest) {
	sender := &wsResponseSender{
		client: c,
	}
	c.wsHandler.OpenTunnel(ctx, wsOpen, sender, req)
}

// handleWSMessage handles an incoming WebSocket message to be forwarded.
//
// Binary address: 0xb62c80
func (c *Client) handleWSMessage(ctx context.Context, wsMsg *tunnelpb.TunnelRequest_WsMessage, req *tunnelpb.TunnelRequest) {
	c.wsHandler.SendMessage(wsMsg, req)
}

// handleWSClose handles a WebSocket tunnel close request.
// Calls CloseTunnel on the WSHandler and logs the result.
//
// Binary address: 0xb62e20
func (c *Client) handleWSClose(ctx context.Context, wsClose *tunnelpb.TunnelRequest_WsClose, req *tunnelpb.TunnelRequest) {
	err := c.wsHandler.CloseTunnel(wsClose)
	if err != nil {
		reason := ""
		if wsClose != nil {
			reason = wsClose.GetReason()
		}

		slog.Warn("ws tunnel close",
			"reason", reason,
			"error", err,
		)
	}
}

// pingLoop sends periodic ping messages to keep the WebSocket connection alive.
// It runs until the context is cancelled.
//
// Binary address: 0xb62fc0
func (c *Client) pingLoop(ctx context.Context, cancel context.CancelCauseFunc, conn *websocket.Conn) error {
	ticker := time.NewTicker(10 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-ticker.C:
			c.mu.Lock()
			deadline := time.Now().Add(10 * time.Second) // 0x2540be400 ns
			err := conn.WriteControl(websocket.PingMessage, nil, deadline)
			c.mu.Unlock()

			if err != nil {
				dogmetrics.Incr(c.metricsClient, "tunnel.ping_failed")
				slog.Warn("tunnel ping failed", "error", err)
				return err
			}
		}
	}
}

// closeConnection closes the WebSocket connection and cleans up resources.
// It first calls CloseAll on the WebSocket handler, then acquires the mutex
// and closes the connection.
//
// Binary address: 0xb63c40
func (c *Client) closeConnection() {
	c.wsHandler.CloseAll()

	c.mu.Lock()
	defer c.mu.Unlock()

	if c.conn != nil {
		c.conn.Close()
		c.conn = nil
	}
}

// connectWithRetries attempts to connect to the tunnel server with exponential
// backoff and jitter. It retries up to 5 times with increasing delays starting
// at 1 second.
//
// Binary address: 0xb63420
func (c *Client) connectWithRetries(ctx context.Context) error {
	attempt := 1
	delay := time.Second // 0x3b9aca00 ns

	for attempt <= 5 {
		slog.Info("connecting to tunnel",
			"attempt", attempt,
			"delay", delay,
			"tunnel_id", c.tunnelID,
		)

		dogmetrics.Incr(c.metricsClient, "tunnel.connect_attempt")

		err := c.Connect(ctx)
		if err == nil {
			return nil
		}

		dogmetrics.Incr(c.metricsClient, "tunnel.connect_failed")

		// Unwrap the error for logging
		errMsg := err.Error()

		slog.Warn("tunnel connect failed",
			"attempt", attempt,
			"error_type", fmt.Sprintf("%T", err),
			"error", errMsg,
			"delay", delay,
			"tunnel_id", c.tunnelID,
		)

		// Check if context was cancelled during connection
		if ctx.Err() != nil {
			return ctx.Err()
		}

		// Wait with jitter before retrying
		jitteredDelay := addJitter(delay)
		timer := time.NewTimer(jitteredDelay)
		select {
		case <-ctx.Done():
			timer.Stop()
			return ctx.Err()
		case <-timer.C:
		}

		attempt++
		delay *= 2 // exponential backoff
	}

	return fmt.Errorf("failed to connect after %d attempts", attempt-1)
}

// ConnectAndRun connects to the tunnel server with retries and then runs
// the main event loop. If the connection drops, it reconnects with backoff.
//
// Binary address: 0xb63da0
func (c *Client) ConnectAndRun(ctx context.Context) error {
	for {
		err := c.connectWithRetries(ctx)
		if err != nil {
			return fmt.Errorf("tunnel connection failed: %w", err)
		}

		err = c.Run(ctx)
		if err != nil {
			if ctx.Err() != nil {
				return ctx.Err()
			}

			slog.Warn("tunnel disconnected, reconnecting",
				"error", err,
				"tunnel_id", c.tunnelID,
			)

			dogmetrics.Incr(c.metricsClient, "tunnel.disconnected")
			c.closeConnection()
			continue
		}

		return nil
	}
}

// Close shuts down the tunnel client. It closes all WebSocket tunnels,
// acquires the mutex, logs the close event, sends a close message on
// the WebSocket connection, and cleans up.
//
// Binary address: 0xb65060
func (c *Client) Close() error {
	c.wsHandler.CloseAll()

	c.mu.Lock()
	defer c.mu.Unlock()

	if c.conn != nil {
		slog.Info("closing tunnel client",
			"tunnel_id", c.tunnelID,
		)

		// Send WebSocket close message
		deadline := time.Now().Add(5 * time.Second)
		c.conn.WriteControl(
			websocket.CloseMessage,
			websocket.FormatCloseMessage(websocket.CloseNormalClosure, ""),
			deadline,
		)

		c.conn.Close()
		c.conn = nil
	}

	return nil
}

// sendResponse serializes a TunnelResponse as protojson and sends it over
// the WebSocket connection. Acquires the client mutex for thread-safe writes.
// Sets a write deadline before sending.
//
// Binary address: 0xb652a0
func (c *Client) sendResponse(resp *tunnelpb.TunnelResponse) error {
	c.mu.Lock()
	defer c.mu.Unlock()

	if c.conn == nil {
		return fmt.Errorf("tunnel connection closed")
	}

	// Set write deadline (10s)
	deadline := time.Now().Add(10 * time.Second) // 0x2540be400 ns
	c.conn.SetWriteDeadline(deadline)

	// Marshal response to protojson
	data, err := protojson.Marshal(resp)
	if err != nil {
		dogmetrics.Incr(c.metricsClient, "tunnel.send_response_marshal_failed")
		slog.Error("failed to marshal tunnel response", "error", err)
		return fmt.Errorf("failed to marshal tunnel response: %w", err)
	}

	// Write the message
	if err := c.conn.WriteMessage(websocket.TextMessage, data); err != nil {
		dogmetrics.Incr(c.metricsClient, "tunnel.send_response_write_failed")
		slog.Error("failed to send tunnel response",
			"error", err,
			"tunnel_id", c.tunnelID,
		)
		return fmt.Errorf("failed to send tunnel response: %w", err)
	}

	return nil
}

// addJitter adds random jitter (up to 50% of the base duration) to a duration.
// Panics if base is <= 0.
//
// Binary address: 0xb64fe0
func addJitter(base time.Duration) time.Duration {
	half := base / 2
	if half <= 0 {
		panic("addJitter: base duration must be positive")
	}
	return base + time.Duration(rand.Int64N(int64(half)))
}

// --- httpResponseSender methods ---

// SendHeaders wraps a TunnelResponse with HttpHeaders payload and sends it.
//
// Binary address: 0xb656a0
func (s *httpResponseSender) SendHeaders(resp *tunnelpb.TunnelResponse) error {
	return s.client.sendResponse(resp)
}

// SendChunk wraps a TunnelResponse with HttpChunk payload and sends it.
//
// Binary address: 0xb65760
func (s *httpResponseSender) SendChunk(resp *tunnelpb.TunnelResponse) error {
	return s.client.sendResponse(resp)
}

// SendError wraps a TunnelResponse with HttpError payload and sends it.
//
// Binary address: 0xb65820
func (s *httpResponseSender) SendError(resp *tunnelpb.TunnelResponse) error {
	return s.client.sendResponse(resp)
}

// --- wsResponseSender methods ---

// SendOpened wraps a TunnelResponse with WsOpened payload and sends it.
//
// Binary address: 0xb658e0
func (s *wsResponseSender) SendOpened(resp *tunnelpb.TunnelResponse) error {
	return s.client.sendResponse(resp)
}

// SendMessage wraps a TunnelResponse with WsMessage payload and sends it.
//
// Binary address: 0xb659a0
func (s *wsResponseSender) SendMessage(resp *tunnelpb.TunnelResponse) error {
	return s.client.sendResponse(resp)
}

// SendClose wraps a TunnelResponse with WsClose payload and sends it.
//
// Binary address: 0xb65a60
func (s *wsResponseSender) SendClose(resp *tunnelpb.TunnelResponse) error {
	return s.client.sendResponse(resp)
}

// SendError wraps a TunnelResponse with WsError payload and sends it.
//
// Binary address: 0xb65b20
func (s *wsResponseSender) SendError(resp *tunnelpb.TunnelResponse) error {
	return s.client.sendResponse(resp)
}

// textprotoCanonicalMIMEHeaderKey is a helper that canonicalizes an HTTP header key.
func textprotoCanonicalMIMEHeaderKey(key string) string {
	// Uses net/textproto.CanonicalMIMEHeaderKey internally
	return key
}

// Ensure Client implements manager.TunnelClient.
var _ manager.TunnelClient = (*Client)(nil)
