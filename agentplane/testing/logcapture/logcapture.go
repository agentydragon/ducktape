// Package logcapture captures the default slog logger in tests.
package logcapture

import (
	"bytes"
	"log/slog"
	"sync"
	"testing"
)

// Buffer collects the lines a log handler writes. Handlers may write from other goroutines.
type Buffer struct {
	mu   sync.Mutex
	data bytes.Buffer
}

func (b *Buffer) Write(p []byte) (int, error) {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.data.Write(p)
}

func (b *Buffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.data.String()
}

// Capture sends the default logger's output, as JSON lines, to a Buffer until the test ends.
func Capture(t testing.TB) *Buffer {
	t.Helper()
	logs := &Buffer{}
	previous := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(logs, nil)))
	t.Cleanup(func() { slog.SetDefault(previous) })
	return logs
}
