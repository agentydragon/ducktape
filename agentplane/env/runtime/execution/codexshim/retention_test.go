package codexshim

import (
	"context"
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/execution/native"
	"github.com/coder/websocket"
)

func TestSessionSurvivesThirtySecondDisconnect(t *testing.T) {
	core := native.New()
	defer core.Close()
	server := httptest.NewServer(Handler{Server: core})
	defer server.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Second)
	defer cancel()
	dial := func() *websocket.Conn {
		t.Helper()
		ws, _, err := websocket.Dial(ctx, "ws"+strings.TrimPrefix(server.URL, "http"), nil)
		if err != nil {
			t.Fatal(err)
		}
		return ws
	}
	call := func(ws *websocket.Conn, id int, method string, params any) map[string]any {
		t.Helper()
		b, _ := json.Marshal(map[string]any{"id": id, "method": method, "params": params})
		if err := ws.Write(ctx, websocket.MessageText, b); err != nil {
			t.Fatal(err)
		}
		for {
			_, b, err := ws.Read(ctx)
			if err != nil {
				t.Fatal(err)
			}
			var v map[string]any
			if err := json.Unmarshal(b, &v); err != nil {
				t.Fatal(err)
			}
			if v["id"] == float64(id) {
				return v
			}
		}
	}
	first := dial()
	init := call(first, 1, "initialize", map[string]any{"clientName": "retention"})
	id := init["result"].(map[string]any)["sessionId"].(string)
	start := call(first, 2, "process/start", map[string]any{"processId": "p", "argv": []string{"/bin/sh", "-c", "sleep 45; printf done"}, "cwd": "file:///tmp", "env": map[string]string{}})
	if start["error"] != nil {
		t.Fatalf("start: %v", start)
	}
	first.CloseNow()
	timer := time.NewTimer(31 * time.Second)
	defer timer.Stop()
	select {
	case <-timer.C:
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	second := dial()
	defer second.CloseNow()
	resumed := call(second, 3, "initialize", map[string]any{"clientName": "retention", "resumeSessionId": id})
	if resumed["error"] != nil {
		t.Fatalf("resume: %v", resumed)
	}
	read := call(second, 4, "process/read", map[string]any{"processId": "p", "maxBytes": 1024, "waitMs": 0})
	if read["error"] != nil {
		t.Fatalf("read after reconnect: %v", read)
	}
	result := read["result"].(map[string]any)
	if result["exited"] != false {
		t.Fatalf("process did not survive: %v", result)
	}
}
