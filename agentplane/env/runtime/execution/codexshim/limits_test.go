package codexshim

import (
	"strings"
	"testing"
)

func TestLargeWriteAndFileLimit(t *testing.T) {
	limits := DefaultLimits()
	limits.MaxOpenFiles = 1
	ctx := t.Context()
	ws := dialShim(t, ctx, serveShim(t, limits))
	if got := shimCall(t, ctx, ws, 1, "initialize", map[string]any{"clientName": "test"}); got["error"] != nil {
		t.Fatal(got)
	}
	path := "file://" + t.TempDir() + "/data"
	if got := shimCall(t, ctx, ws, 2, "fs/open", map[string]any{"path": path, "handleId": "one", "mode": "replace"}); got["error"] != nil {
		t.Fatal(got)
	}
	if got := shimCall(t, ctx, ws, 3, "fs/open", map[string]any{"path": path, "handleId": "two", "mode": "replace"}); got["error"] == nil {
		t.Fatal("expected configured handle limit")
	}
	chunk := []byte(strings.Repeat("x", 1<<20))
	if got := shimCall(t, ctx, ws, 4, "fs/writeBlock", map[string]any{"handleId": "one", "offset": 0, "chunk": chunk}); got["error"] != nil {
		t.Fatal(got)
	}
	if got := shimCall(t, ctx, ws, 5, "fs/close", map[string]any{"handleId": "one"}); got["error"] != nil {
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
