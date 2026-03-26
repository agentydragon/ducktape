package logger

import (
	"bytes"
	"context"
	"log/slog"
)

// LogWriter implements io.Writer and forwards written bytes as slog log
// records. It buffers input until newlines are found, then logs each
// complete line. If the buffer grows beyond 1 MB without a newline, the
// buffer contents are flushed with a truncation notice.
type LogWriter struct {
	logger *slog.Logger
	ctx    context.Context
	level  slog.Level
	prefix string
	attrs  []slog.Attr
	buf    []byte
}

// Write appends p to the internal buffer, then logs each complete line
// (delimited by newline). Returns len(p) and nil on success.
func (w *LogWriter) Write(p []byte) (int, error) {
	w.buf = append(w.buf, p...)

	for {
		idx := bytes.IndexByte(w.buf, '\n')
		if idx == -1 {
			break
		}

		line := string(w.buf[:idx])
		w.buf = w.buf[idx+1:]

		if len(line) > 0 {
			w.logger.LogAttrs(w.ctx, w.level, w.prefix+line, w.attrs...)
		}
	}

	if len(w.buf) > 1048576 {
		w.logger.LogAttrs(w.ctx, w.level, w.prefix+string(w.buf)+" [line truncated - no newline]", w.attrs...)
		w.buf = nil
	}

	return len(p), nil
}
