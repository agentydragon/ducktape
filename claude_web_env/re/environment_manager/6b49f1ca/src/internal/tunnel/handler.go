// Reconstructed from environment-manager binary (Build ID: 6b49f1ca)
// Source: internal/tunnel/handler.go
// Module: github.com/anthropics/anthropic/api-go/environment-manager

package tunnel

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"time"

	isync "sync"

	tunnelpb "github.com/anthropics/anthropic/api-go/environment-manager/internal/tunnel/tunnelpb"
)

// Handler handles incoming HTTP tunnel requests by forwarding them to a local
// server and streaming responses back through the tunnel.
//
// Uses internal/sync.HashTrieMap for concurrent request tracking (cancel funcs).
type Handler struct {
	logger    *slog.Logger
	baseURL   string
	requests  map[string]interface{}
	cancelMap isync.Map // offset 0x18 - stores context.CancelFunc values
}

// HandleRequest forwards an incoming HTTP tunnel request to the local server.
// It creates an HTTP request from the tunnel request fields, tracks the
// request for cancellation using a HashTrieMap, executes the request
// against the local server, and streams the response back via the
// ResponseSender.
//
// Logs the request with: path, port, method, url, content_type, header_count.
//
// Binary address: 0xb65be0
func (h *Handler) HandleRequest(
	ctx context.Context,
	req *tunnelpb.HTTPTunnelRequest,
	sender ResponseSender,
) error {
	startTime := time.Now()

	// Extract request metadata
	var path, method, url string
	var port int32
	var headerCount int
	var body []byte

	if req != nil {
		path = req.GetPath()
		port = req.GetPort()
		method = req.GetMethod()
		url = req.GetUrl()
		body = req.GetBody()
		headerCount = len(req.GetHeaders())
	}

	slog.Info("handling http request",
		"path", path,
		"port", port,
		"method", method,
		"url", url,
		"header_count", headerCount,
	)

	// Validate port range
	if port-1 > 0xFFFE {
		// Port out of valid range - construct URL differently
		// Falls through to the URL construction below
	}

	// Construct the local URL
	localURL := fmt.Sprintf("http://localhost:%d", port) + path

	// Build HTTP request
	var bodyReader io.Reader
	if len(body) > 0 {
		// Create reader from body bytes
	}

	httpReq, err := http.NewRequestWithContext(ctx, method, localURL, bodyReader)
	if err != nil {
		return fmt.Errorf("failed to create upstream HTTP request: %w", err)
	}

	// Copy headers from tunnel request (repeated Header messages)
	if req != nil {
		for _, h := range req.GetHeaders() {
			httpReq.Header.Add(h.GetName(), h.GetValue())
		}
	}

	// Create cancellable context and track it
	reqCtx, cancel := context.WithCancel(ctx)
	defer cancel()

	requestID := req.GetRequestId()
	h.cancelMap.Store(requestID, cancel)
	defer h.cancelMap.Delete(requestID)

	httpReq = httpReq.WithContext(reqCtx)

	// Execute the HTTP request against the local server
	client := &http.Client{}
	resp, err := client.Do(httpReq)
	if err != nil {
		return fmt.Errorf("failed to execute upstream HTTP request: %w", err)
	}
	defer resp.Body.Close()

	// Send headers back through tunnel
	respHeaders := &tunnelpb.HttpHeaders{
		StatusCode: int32(resp.StatusCode),
	}
	headersResp := &tunnelpb.TunnelResponse{
		Payload: &tunnelpb.TunnelResponse_HttpHeaders{
			HttpHeaders: respHeaders,
		},
	}
	if err := sender.SendHeaders(headersResp); err != nil {
		return err
	}

	// Stream body chunks
	buf := make([]byte, 32*1024) // 32KB buffer
	for {
		n, readErr := resp.Body.Read(buf)
		if n > 0 {
			chunk := &tunnelpb.HttpChunk{
				Data: buf[:n],
			}
			chunkResp := &tunnelpb.TunnelResponse{
				Payload: &tunnelpb.TunnelResponse_HttpChunk{
					HttpChunk: chunk,
				},
			}
			if err := sender.SendChunk(chunkResp); err != nil {
				return err
			}
		}
		if readErr == io.EOF {
			break
		}
		if readErr != nil {
			return readErr
		}
	}

	elapsed := time.Since(startTime)
	slog.Info("completed http request",
		"path", path,
		"status", resp.StatusCode,
		"duration", elapsed,
		"url", url,
		"request_id", req.GetRequestId(),
	)

	return nil
}

// CancelRequest cancels an in-flight HTTP request tracked by request ID.
// Uses HashTrieMap.Load to find the cancel function, calls it, then
// uses LoadAndDelete to remove the entry.
//
// Binary address: 0xb678a0
func (h *Handler) CancelRequest(requestID string) {
	// Load the cancel function from the map
	cancelFunc, ok := h.cancelMap.Load(requestID)
	if !ok {
		return
	}

	// Call the cancel function with type assertion
	if fn, ok := cancelFunc.(context.CancelFunc); ok {
		fn()
	}

	// Remove the entry from the map
	h.cancelMap.LoadAndDelete(requestID)
}
