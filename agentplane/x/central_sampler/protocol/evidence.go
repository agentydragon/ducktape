package protocol

import (
	"context"
	"io"
	"unicode/utf8"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
)

const (
	DisplayBytes           = 256 * 1024
	MaxProviderStreamBytes = 64 * 1024 * 1024
)

// EvidenceSink must persist the native event and its observations atomically before returning.
// A failure stops stream processing and leaves dispatch acknowledgement unresolved.
type EvidenceSink interface {
	Record(context.Context, string, map[string]any, ...*eventpb.Event) error
}

// CaptureReader retains transport bytes before parsing, including incomplete SSE records.
// JSON represents []byte as base64, preserving split UTF-8 sequences without replacement.
type CaptureReader struct {
	Reader   io.Reader
	Context  context.Context
	Evidence EvidenceSink
}

func (r CaptureReader) Read(buffer []byte) (int, error) {
	n, err := r.Reader.Read(buffer)
	if n > 0 && r.Evidence != nil {
		if e := r.Evidence.Record(r.Context, "provider_wire", map[string]any{"bytes": buffer[:n]}); e != nil {
			return 0, e
		}
	}
	return n, err
}

func DisplayText(value string) string {
	encoded := []byte(value)
	if len(encoded) <= DisplayBytes {
		return value
	}
	cut := encoded[:DisplayBytes]
	for len(cut) > 0 && !utf8.Valid(cut) {
		cut = cut[:len(cut)-1]
	}
	return string(cut) + "\n[Display truncated; full content is retained in source boundary records.]"
}
