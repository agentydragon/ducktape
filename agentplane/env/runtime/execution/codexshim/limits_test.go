package codexshim

import (
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/native"
	"github.com/coder/websocket"
)

func TestLargeWriteAndFileLimit(t *testing.T) {
	core := native.New()
	defer core.Close()
	limits := DefaultLimits()
	limits.MaxOpenFiles = 1
	srv := httptest.NewServer(Handler{Server: core, Limits: limits})
	defer srv.Close()
	ctx := t.Context()
	ws, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(srv.URL, "http"), nil)
	if err != nil {
		t.Fatal(err)
	}
	defer ws.CloseNow()
	call := func(id int, method string, params any) map[string]any {
		t.Helper()
		data, _ := json.Marshal(map[string]any{"id": id, "method": method, "params": params})
		if err := ws.Write(ctx, websocket.MessageText, data); err != nil {
			t.Fatal(err)
		}
		_, data, err := ws.Read(ctx)
		if err != nil {
			t.Fatal(err)
		}
		var result map[string]any
		if err := json.Unmarshal(data, &result); err != nil {
			t.Fatal(err)
		}
		return result
	}
	if got := call(1, "initialize", map[string]any{"clientName": "test"}); got["error"] != nil {
		t.Fatal(got)
	}
	path := "file://" + t.TempDir() + "/data"
	if got := call(2, "fs/open", map[string]any{"path": path, "handleId": "one", "mode": "replace"}); got["error"] != nil {
		t.Fatal(got)
	}
	if got := call(3, "fs/open", map[string]any{"path": path, "handleId": "two", "mode": "replace"}); got["error"] == nil {
		t.Fatal("expected configured handle limit")
	}
	chunk := []byte(strings.Repeat("x", 1<<20))
	if got := call(4, "fs/writeBlock", map[string]any{"handleId": "one", "offset": 0, "chunk": chunk}); got["error"] != nil {
		t.Fatal(got)
	}
	if got := call(5, "fs/close", map[string]any{"handleId": "one"}); got["error"] != nil {
		t.Fatal(got)
	}
}

func TestInvalidLimits(t *testing.T) {
	limits := DefaultLimits()
	limits.MaxMessageBytes = 0
	if limits.Validate() == nil {
		t.Fatal("zero limit accepted")
	}
}
