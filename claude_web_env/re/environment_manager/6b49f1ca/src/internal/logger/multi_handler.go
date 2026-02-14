package logger

import (
	"context"
	"fmt"
	"log/slog"
	"os"
)

// MultiHandler is a slog.Handler that fans out log records to multiple
// underlying handlers.
type MultiHandler struct {
	handlers []slog.Handler
}

// Enabled reports whether any of the underlying handlers is enabled for the
// given level.
func (m *MultiHandler) Enabled(ctx context.Context, level slog.Level) bool {
	for _, h := range m.handlers {
		if h.Enabled(ctx, level) {
			return true
		}
	}
	return false
}

// Handle sends the record to every underlying handler that is enabled for the
// record's level. Errors from individual handlers are logged to stderr but do
// not stop processing.
func (m *MultiHandler) Handle(ctx context.Context, r slog.Record) error {
	for _, h := range m.handlers {
		if !h.Enabled(ctx, r.Level) {
			continue
		}
		if err := h.Handle(ctx, r); err != nil {
			fmt.Fprintf(os.Stderr, "handler error: %v\n", err)
		}
	}
	return nil
}

// WithAttrs returns a new MultiHandler where each underlying handler has the
// given attributes applied.
func (m *MultiHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	handlers := make([]slog.Handler, len(m.handlers))
	for i, h := range m.handlers {
		handlers[i] = h.WithAttrs(attrs)
	}
	return &MultiHandler{handlers: handlers}
}

// WithGroup returns a new MultiHandler where each underlying handler uses the
// given group name.
func (m *MultiHandler) WithGroup(name string) slog.Handler {
	handlers := make([]slog.Handler, len(m.handlers))
	for i, h := range m.handlers {
		handlers[i] = h.WithGroup(name)
	}
	return &MultiHandler{handlers: handlers}
}
