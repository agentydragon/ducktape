package codexshim

import (
	"context"
	"testing"
	"time"
)

func TestSessionSurvivesThirtySecondDisconnect(t *testing.T) {
	wsURL := serveShim(t, DefaultLimits())
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Second)
	defer cancel()
	first := dialShim(t, ctx, wsURL)
	init := shimCall(t, ctx, first, 1, "initialize", map[string]any{"clientName": "retention"})
	id := init["result"].(map[string]any)["sessionId"].(string)
	start := shimCall(t, ctx, first, 2, "process/start", map[string]any{"processId": "p", "argv": []string{"/bin/sh", "-c", "sleep 45; printf done"}, "cwd": "file:///tmp", "env": map[string]string{}})
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
	second := dialShim(t, ctx, wsURL)
	resumed := shimCall(t, ctx, second, 3, "initialize", map[string]any{"clientName": "retention", "resumeSessionId": id})
	if resumed["error"] != nil {
		t.Fatalf("resume: %v", resumed)
	}
	read := shimCall(t, ctx, second, 4, "process/read", map[string]any{"processId": "p", "maxBytes": 1024, "waitMs": 0})
	if read["error"] != nil {
		t.Fatalf("read after reconnect: %v", read)
	}
	result := read["result"].(map[string]any)
	if result["exited"] != false {
		t.Fatalf("process did not survive: %v", result)
	}
}
