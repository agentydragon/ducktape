package execserver

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/runner/supervision"
	"github.com/coder/websocket"
)

func TestProbeRequiresValidRPCAndBoundsUnresponsiveServer(t *testing.T) {
	for _, test := range []struct {
		name, response string
		success        bool
	}{
		{"healthy", `{"id":1,"result":{"sessionId":"test-session"}}`, true},
		{"RPC error", `{"id":1,"error":{"code":-32603,"message":"unavailable"}}`, false},
		{"wrong request", `{"id":2,"result":{"sessionId":"test-session"}}`, false},
		{"stalled dispatcher", "", false},
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
				if test.success {
					if _, _, err := connection.Read(ctx); err != nil {
						return
					}
					if _, _, err := connection.Read(ctx); err != nil {
						return
					}
					_ = connection.Write(ctx, websocket.MessageText, []byte(`{"id":2,"result":{"status":"ready"}}`))
				}
				_, _, _ = connection.Read(ctx)
			}))
			defer server.Close()
			ctx, cancel := context.WithTimeout(context.Background(), 100*time.Millisecond)
			defer cancel()
			err := ProbeAt("ws" + strings.TrimPrefix(server.URL, "http"))(ctx)
			if (err == nil) != test.success {
				t.Fatalf("probe = %v, success=%t", err, test.success)
			}
		})
	}
}

func TestConfigurationRejectsPartialOrOverlappingIsolation(t *testing.T) {
	for _, endpoint := range []string{"", "ws://0.0.0.0:4501", "ws://127.0.0.1:0", "wss://127.0.0.1:4501", "ws://127.0.0.1:4501/path", "ws://user@127.0.0.1:4501"} {
		if err := (Config{URL: endpoint}).Validate(nil); err == nil {
			t.Fatalf("accepted %q", endpoint)
		}
	}
	const endpoint = "ws://127.0.0.1:4501"
	if err := (Config{URL: endpoint, ContainerSocket: "/run/test-executor.sock"}).Validate(nil); err != nil {
		t.Fatal(err)
	}
	limits := supervision.ResourceLimits{CPUQuotaMicros: 100000, MemoryMaxBytes: 64 * 1024 * 1024, PidsMax: 32, ReadBPS: 1, WriteBPS: 1, ReadIOPS: 1, WriteIOPS: 1}
	harnessGroups := &supervision.SystemdPolicy{Slice: supervision.WorkloadSlice, AggregateLimits: limits, ProcessLimits: limits}
	executorGroups := *harnessGroups
	executorGroups.Slice = supervision.ExecutorSlice
	if err := (Config{URL: endpoint, Isolation: &executorGroups}).Validate(harnessGroups); err != nil {
		t.Fatal(err)
	}
	invalid := executorGroups
	invalid.ProcessLimits.MemoryMaxBytes++
	for _, config := range []Config{
		{URL: endpoint},
		{URL: endpoint, ContainerSocket: "relative"},
		{URL: endpoint, ContainerSocket: "/run/test.sock", Isolation: harnessGroups},
		{URL: endpoint, Isolation: harnessGroups},
		{URL: endpoint, Isolation: &supervision.SystemdPolicy{Slice: "invalid.slice"}},
		{URL: endpoint, Isolation: &invalid},
	} {
		if err := config.Validate(harnessGroups); err == nil {
			t.Fatalf("accepted %+v", config)
		}
	}
}
