package logger

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// FileLoggingHandler implements slog.Handler and writes structured log entries
// to a file. It wraps an inner slog.Handler, delegating Enabled/WithAttrs/WithGroup
// to it, while also writing its own JSON-formatted log entries to a file in Handle.
type FileLoggingHandler struct {
	inner    slog.Handler
	file     *os.File
	mu       *sync.Mutex
	minLevel slog.Level
}

// NewFileLoggingHandler creates a new FileLoggingHandler that writes to the
// specified file path. The inner handler is used for delegation. minLevel
// controls the minimum log level for file output.
func NewFileLoggingHandler(inner slog.Handler, filePath string, minLevel slog.Level) (*FileLoggingHandler, error) {
	dir := filepath.Dir(filePath)
	if dir != "/" && dir != "." {
		if err := os.MkdirAll(dir, 0o755); err != nil {
			return nil, fmt.Errorf("failed to create log directory: %w", err)
		}
	}

	file, err := os.OpenFile(filePath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return nil, fmt.Errorf("failed to open log file: %w", err)
	}

	return &FileLoggingHandler{
		inner:    inner,
		file:     file,
		mu:       &sync.Mutex{},
		minLevel: minLevel,
	}, nil
}

// Enabled reports whether the handler handles records at the given level.
func (h *FileLoggingHandler) Enabled(ctx context.Context, level slog.Level) bool {
	return h.inner.Enabled(ctx, level)
}

// Handle processes the log record by delegating to the inner handler, then
// writing a structured JSON entry to the log file.
func (h *FileLoggingHandler) Handle(ctx context.Context, r slog.Record) error {
	if err := h.inner.Handle(ctx, r); err != nil {
		return fmt.Errorf("inner handler error: %w", err)
	}
	h.writeToFile(ctx, r)
	return nil
}

// WithAttrs returns a new FileLoggingHandler whose inner handler includes
// the given attributes.
func (h *FileLoggingHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	return &FileLoggingHandler{
		inner:    h.inner.WithAttrs(attrs),
		file:     h.file,
		mu:       h.mu,
		minLevel: h.minLevel,
	}
}

// WithGroup returns a new FileLoggingHandler whose inner handler uses the
// given group name.
func (h *FileLoggingHandler) WithGroup(name string) slog.Handler {
	return &FileLoggingHandler{
		inner:    h.inner.WithGroup(name),
		file:     h.file,
		mu:       h.mu,
		minLevel: h.minLevel,
	}
}

// writeToFile marshals the record into a JSON map and writes it to the log file.
func (h *FileLoggingHandler) writeToFile(ctx context.Context, r slog.Record) {
	entry := make(map[string]interface{})

	entry["timestamp"] = time.Now().Format(time.RFC3339Nano)
	entry["level"] = r.Level.String()
	entry["message"] = r.Message
	entry["time"] = r.Time.Format(time.RFC3339Nano)

	attrs := make(map[string]interface{})
	r.Attrs(func(a slog.Attr) bool {
		attrs[a.Key] = a.Value.Any()
		return true
	})

	if len(attrs) > 0 {
		entry["attributes"] = attrs
	}

	data, err := json.Marshal(entry)
	if err != nil {
		return
	}

	h.mu.Lock()
	defer h.mu.Unlock()

	if _, err := h.file.Write(data); err != nil {
		fmt.Fprintf(os.Stderr, "Failed to write log data: %v\n", err)
	}

	if _, err := h.file.Write([]byte("\n")); err != nil {
		fmt.Fprintf(os.Stderr, "Failed to write newline: %v\n", err)
	}
}

// Close closes the underlying log file.
func (h *FileLoggingHandler) Close() error {
	h.mu.Lock()
	defer h.mu.Unlock()

	if err := h.file.Close(); err != nil {
		return fmt.Errorf("failed to close log file: %w", err)
	}
	return nil
}

// CreateLoggerWithFileOutput creates a slog.Logger that writes to both stderr
// (as JSON) and a log file. If the file handler cannot be created, it falls
// back to stderr-only logging.
func CreateLoggerWithFileOutput(minLevel slog.Level) *slog.Logger {
	stderrHandler := slog.NewJSONHandler(os.Stderr, &slog.HandlerOptions{
		Level: minLevel,
	})

	fileHandler, err := NewFileLoggingHandler(stderrHandler, "/tmp/env-manager.log", minLevel)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Warning: Failed to create file logger: %v\n", err)
		return slog.New(stderrHandler)
	}

	return slog.New(fileHandler)
}
