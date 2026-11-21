package codexshim

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/native"
	"github.com/coder/websocket"
)

// serveShim serves a Handler with the given limits over a fresh native server and returns its WebSocket URL.
func serveShim(t *testing.T, limits Limits) string {
	t.Helper()
	core := native.New()
	t.Cleanup(func() { _ = core.Close() })
	server := httptest.NewServer(Handler{Server: core, Limits: limits})
	t.Cleanup(server.Close)
	return "ws" + strings.TrimPrefix(server.URL, "http")
}

func dialShim(t *testing.T, ctx context.Context, wsURL string) *websocket.Conn {
	t.Helper()
	ws, _, err := websocket.Dial(ctx, wsURL, nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = ws.CloseNow() })
	return ws
}

func readShim(t *testing.T, ctx context.Context, ws *websocket.Conn) map[string]any {
	t.Helper()
	_, b, err := ws.Read(ctx)
	if err != nil {
		t.Fatal(err)
	}
	var v map[string]any
	if err := json.Unmarshal(b, &v); err != nil {
		t.Fatal(err)
	}
	return v
}

// shimCall sends one request and returns the reply with the same id, skipping notifications that arrive first.
func shimCall(t *testing.T, ctx context.Context, ws *websocket.Conn, id int, method string, params any) map[string]any {
	t.Helper()
	b, err := json.Marshal(map[string]any{"id": id, "method": method, "params": params})
	if err != nil {
		t.Fatal(err)
	}
	if err := ws.Write(ctx, websocket.MessageText, b); err != nil {
		t.Fatal(err)
	}
	for {
		if v := readShim(t, ctx, ws); v["id"] == float64(id) {
			return v
		}
	}
}
