package adapter

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/coder/websocket"
)

func TestProbeRequiresValidRPCAndBoundsUnresponsiveServer(t *testing.T) {
	for _, test := range []struct {
		name, response, status, wantError string
	}{
		{"healthy", `{"id":1,"result":{"sessionId":"test-session"}}`, `{"id":2,"result":{"status":"ready"}}`, ""},
		{"RPC error", `{"id":1,"error":{"code":-32603,"message":"unavailable"}}`, "", "executor health initialize returned no session"},
		{"wrong request", `{"id":2,"result":{"sessionId":"test-session"}}`, "", "executor health initialize returned no session"},
		{"stalled dispatcher", "", "", "executor initialize read"},
		{"invalid initialize JSON", "{", "", "executor initialize decode"},
		{"stalled status", `{"id":1,"result":{"sessionId":"test-session"}}`, "", "executor environment/status read"},
		{"invalid status JSON", `{"id":1,"result":{"sessionId":"test-session"}}`, "{", "executor environment/status decode"},
		{"not ready", `{"id":1,"result":{"sessionId":"test-session"}}`, `{"id":2,"result":{"status":"starting"}}`, `executor environment/status returned id=2 status="starting"`},
	} {
		t.Run(test.name, func(t *testing.T) {
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				connection, err := websocket.Accept(w, r, nil)
				if err != nil {
					return
				}
				defer connection.CloseNow()
				ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
				defer cancel()
				if _, _, err := connection.Read(ctx); err != nil {
					return
				}
				if test.response != "" {
					_ = connection.Write(ctx, websocket.MessageText, []byte(test.response))
				}
				if _, _, err := connection.Read(ctx); err != nil {
					return
				}
				if _, _, err := connection.Read(ctx); err != nil {
					return
				}
				if test.status != "" {
					_ = connection.Write(ctx, websocket.MessageText, []byte(test.status))
				}
				_, _, _ = connection.Read(ctx)
			}))
			defer server.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
			defer cancel()
			err := ProbeAt("ws" + strings.TrimPrefix(server.URL, "http"))(ctx)
			if test.wantError == "" && err != nil {
				t.Fatalf("probe = %v, want success", err)
			}
			if test.wantError != "" && (err == nil || !strings.Contains(err.Error(), test.wantError)) {
				t.Fatalf("probe = %v, want error containing %q", err, test.wantError)
			}
		})
	}
}
