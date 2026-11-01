package codexexecserver

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/supervision"
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

func TestConfigurationRejectsPartialOrOverlappingLaunchers(t *testing.T) {
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
	harnessLauncher := &supervision.SystemdLauncher{Slice: supervision.WorkloadSlice, AggregateLimits: limits, ProcessLimits: limits}
	executorLauncher := *harnessLauncher
	executorLauncher.Slice = supervision.ExecutorSlice
	if err := (Config{URL: endpoint, Launcher: &executorLauncher}).Validate(harnessLauncher); err != nil {
		t.Fatal(err)
	}
	invalidLauncher := executorLauncher
	invalidLauncher.ProcessLimits.MemoryMaxBytes++
	for _, config := range []Config{
		{URL: endpoint},
		{URL: endpoint, ContainerSocket: "relative"},
		{URL: endpoint, ContainerSocket: "/run/test.sock", Launcher: harnessLauncher},
		{URL: endpoint, Launcher: harnessLauncher},
		{URL: endpoint, Launcher: &supervision.SystemdLauncher{Slice: "invalid.slice"}},
		{URL: endpoint, Launcher: &invalidLauncher},
	} {
		if err := config.Validate(harnessLauncher); err == nil {
			t.Fatalf("accepted %+v", config)
		}
	}
}
