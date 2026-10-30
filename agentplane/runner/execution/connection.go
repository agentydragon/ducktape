package execution

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sync"

	executorpb "github.com/agentydragon/ducktape/agentplane/executor/executorpb"
	"github.com/coder/websocket"
)

const maxQueuedNotifications = 4

type nativeRequestLimitError struct{}

func (e *nativeRequestLimitError) Error() string {
	return "native execution request exceeds the 1 MiB encoded payload limit before send"
}

type nativeFrame struct {
	payload []byte
}

type connection struct {
	scope         *executorpb.ExecutionScope
	id            string
	slotID        uint64
	ws            *websocket.Conn
	ctx           context.Context
	cancel        context.CancelFunc
	done          <-chan struct{}
	rpcMu         sync.Mutex
	pendingMu     sync.Mutex
	pending       chan nativeFrame
	notifications chan []byte
	calls         chan struct{}
	terminalMu    sync.Mutex
	terminalErr   error
	terminalRaw   []byte
}

func (c *connection) stop() {
	c.cancel()
	_ = c.ws.CloseNow()
}

func (c *connection) fail(err error, raw []byte) {
	c.terminalMu.Lock()
	if c.terminalErr == nil {
		c.terminalErr = err
		c.terminalRaw = append([]byte(nil), raw...)
	}
	c.terminalMu.Unlock()
	c.stop()
}

func (c *connection) failure() (error, []byte) {
	c.terminalMu.Lock()
	defer c.terminalMu.Unlock()
	return c.terminalErr, append([]byte(nil), c.terminalRaw...)
}

// read routes JSON-RPC replies to the one serialized request and applies bounded
// backpressure to notifications consumed by the held OpenSession stream.
func (c *connection) read() {
	defer c.stop()
	for {
		kind, payload, err := c.ws.Read(c.ctx)
		if err != nil {
			c.fail(fmt.Errorf("read local exec-server WebSocket frame: %w", err), payload)
			return
		}
		if kind != websocket.MessageText {
			c.fail(errors.New("native executor sent a non-text WebSocket frame"), payload)
			return
		}
		if len(payload) > maxNativeBytes {
			c.fail(fmt.Errorf("local exec-server WebSocket frame is %d bytes; limit is %d", len(payload), maxNativeBytes), payload)
			return
		}
		var header struct {
			ID     json.RawMessage `json:"id"`
			Method string          `json:"method"`
		}
		if err := decodeProtocolObject(payload, &header); err != nil {
			c.fail(fmt.Errorf("decode local exec-server WebSocket frame: %w", err), payload)
			return
		}
		if len(header.ID) == 0 || bytes.Equal(bytes.TrimSpace(header.ID), []byte("null")) {
			if header.Method == "" {
				c.fail(errors.New("local exec-server notification omitted method"), payload)
				return
			}
			if !c.queueNotification(payload) {
				return
			}
			continue
		}
		c.pendingMu.Lock()
		pending := c.pending
		c.pendingMu.Unlock()
		if pending == nil {
			c.fail(errors.New("local exec-server reply arrived without a pending request"), payload)
			return
		}
		select {
		case pending <- nativeFrame{payload: append([]byte(nil), payload...)}:
		default:
			c.fail(errors.New("native executor sent more than one reply for a request"), payload)
			return
		}
	}
}

func (c *connection) queueNotification(payload []byte) bool {
	select {
	case c.notifications <- append([]byte(nil), payload...):
		return true
	case <-c.ctx.Done():
		return false
	}
}

func (c *connection) rpc(requestID, method string, params any, frames *[]*executorpb.ExecutionNativeFrame) (json.RawMessage, error) {
	c.rpcMu.Lock()
	defer c.rpcMu.Unlock()
	if err := c.ctx.Err(); err != nil {
		return nil, err
	}
	payload, err := json.Marshal(executorRequest[any]{ID: requestID, Method: method, Params: params})
	if err != nil {
		return nil, err
	}
	if len(payload) > maxNativeBytes {
		return nil, &nativeRequestLimitError{}
	}
	*frames = append(*frames, &executorpb.ExecutionNativeFrame{Direction: "request", Payload: payload})
	reply := make(chan nativeFrame, 1)
	c.pendingMu.Lock()
	if c.pending != nil {
		c.pendingMu.Unlock()
		return nil, errors.New("native execution already has a pending request")
	}
	c.pending = reply
	c.pendingMu.Unlock()
	defer func() {
		c.pendingMu.Lock()
		if c.pending == reply {
			c.pending = nil
		}
		c.pendingMu.Unlock()
	}()
	if err = c.ws.Write(c.ctx, websocket.MessageText, payload); err != nil {
		wrapped := fmt.Errorf("write local exec-server RPC request: %w", err)
		c.fail(wrapped, nil)
		return nil, wrapped
	}
	select {
	case native := <-reply:
		*frames = append(*frames, &executorpb.ExecutionNativeFrame{Direction: "response", Payload: native.payload})
		var response executorReply
		if err := decodeProtocolObject(native.payload, &response); err != nil {
			wrapped := fmt.Errorf("decode local exec-server RPC reply: %w", err)
			c.fail(wrapped, nil)
			return nil, wrapped
		}
		if response.ID == nil || *response.ID != requestID {
			err := fmt.Errorf("local exec-server reply ID did not match request %q", requestID)
			c.fail(err, nil)
			return nil, err
		}
		if response.Error != nil {
			return nil, &executorRejectedError{method: method, code: response.Error.Code, message: response.Error.Message}
		}
		if response.Result == nil {
			err := errors.New("local exec-server reply omitted result")
			c.fail(err, nil)
			return nil, err
		}
		return response.Result, nil
	case <-c.ctx.Done():
		select {
		case native := <-reply:
			*frames = append(*frames, &executorpb.ExecutionNativeFrame{Direction: "response", Payload: native.payload})
		default:
		}
		err, _ := c.failure()
		if err != nil {
			return nil, fmt.Errorf("local exec-server call ended: %w", err)
		}
		return nil, c.ctx.Err()
	}
}

func (c *connection) initialized(frames *[]*executorpb.ExecutionNativeFrame) error {
	payload, err := json.Marshal(executorRequest[executorEmpty]{Method: "initialized", Params: executorEmpty{}})
	if err != nil {
		return err
	}
	*frames = append(*frames, &executorpb.ExecutionNativeFrame{Direction: "request", Payload: payload})
	return c.ws.Write(c.ctx, websocket.MessageText, payload)
}

func rpcResult[T any](c *connection, requestID, method string, params any, frames *[]*executorpb.ExecutionNativeFrame) (*T, error) {
	raw, err := c.rpc(requestID, method, params, frames)
	if err != nil {
		return nil, err
	}
	var result T
	if err = decodeProtocolObject(raw, &result); err != nil {
		wrapped := fmt.Errorf("decode local exec-server %s result: %w", method, err)
		c.fail(wrapped, nil)
		return nil, wrapped
	}
	return &result, nil
}
