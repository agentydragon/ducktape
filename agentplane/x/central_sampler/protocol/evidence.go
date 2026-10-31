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
	Record(context.Context, ProviderEvidence, ...*eventpb.Event) error
}

// ProviderEvidenceKind is the boundary a provider adapter's evidence is recorded under.
type ProviderEvidenceKind string

const (
	EvidenceProviderWire ProviderEvidenceKind = "provider_wire"
	EvidenceProviderHTTP ProviderEvidenceKind = "provider_http"
	// EvidenceResponsesEvent is one Responses SSE event; EvidenceAnthropicEvent one Messages SSE event.
	EvidenceResponsesEvent ProviderEvidenceKind = "provider"
	EvidenceAnthropicEvent ProviderEvidenceKind = "provider_anthropic"
)

// ProviderEvidence is a JSON evidence payload; its type determines its boundary.
type ProviderEvidence interface {
	Kind() ProviderEvidenceKind
}

// WireBytes are raw transport bytes, base64 in JSON so split UTF-8 sequences survive.
type WireBytes struct {
	Bytes []byte `json:"bytes"`
}

func (WireBytes) Kind() ProviderEvidenceKind { return EvidenceProviderWire }

// HTTPResponseHead is the status and identity of a provider HTTP response.
type HTTPResponseHead struct {
	ContentType       string `json:"content_type"`
	HTTPStatus        int    `json:"http_status"`
	ProviderRequestID string `json:"provider_request_id"`
}

func (HTTPResponseHead) Kind() ProviderEvidenceKind { return EvidenceProviderHTTP }

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
		if e := r.Evidence.Record(r.Context, WireBytes{Bytes: buffer[:n]}); e != nil {
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
