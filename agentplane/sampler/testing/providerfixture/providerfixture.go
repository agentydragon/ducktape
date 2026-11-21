// Package providerfixture holds the streaming-server, evidence and content-store fakes that the
// provider packages' tests share.
package providerfixture

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/content"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerio"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/testing/jsonassert"
)

// SSEStream writes server-sent events to one response and flushes after each, so the client
// receives them as they are sent.
type SSEStream struct {
	w       http.ResponseWriter
	flusher http.Flusher
}

// Data sends one event carrying payload.
func (s SSEStream) Data(payload string) { s.send("data: " + payload) }

// Comment sends a comment line, which SSE clients ignore.
func (s SSEStream) Comment(text string) { s.send(": " + text) }

func (s SSEStream) send(line string) {
	_, _ = io.WriteString(s.w, line+"\n\n")
	s.flusher.Flush()
}

// SSEServer starts a server that answers every request with an event stream written by handle,
// and closes it when the test ends.
func SSEServer(t testing.TB, handle func(stream SSEStream, r *http.Request)) *httptest.Server {
	t.Helper()
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("content-type", "text/event-stream")
		handle(SSEStream{w: w, flusher: w.(http.Flusher)}, r)
	}))
	t.Cleanup(server.Close)
	return server
}

// IdleBudget starts a semantic-idle budget for one provider call and counts the durable progress it
// reports. The budget stops when the test ends.
func IdleBudget(t testing.TB, idle time.Duration) (context.Context, protocol.ProviderHooks, *atomic.Int32) {
	t.Helper()
	progress := new(atomic.Int32)
	ctx, hooks, stop := providerio.WithProviderIdleTimeout(t.Context(), idle, func(context.Context) error {
		progress.Add(1)
		return nil
	})
	t.Cleanup(stop)
	return ctx, hooks, progress
}

// EvidenceRecorder keeps the evidence one provider call records.
type EvidenceRecorder struct {
	HeadRecorded bool
	Wire         []byte
	// Fail, when set, rejects evidence before it is kept, as a durable store that cannot write would.
	Fail func(protocol.ProviderEvidence) error
}

func (r *EvidenceRecorder) Record(_ context.Context, evidence protocol.ProviderEvidence, _ ...*eventpb.Event) error {
	if r.Fail != nil {
		if err := r.Fail(evidence); err != nil {
			return err
		}
	}
	switch value := evidence.(type) {
	case protocol.HTTPResponseHead:
		r.HeadRecorded = true
	case protocol.WireBytes:
		r.Wire = append(r.Wire, value.Bytes...)
	}
	return nil
}

// AssertJSONEqual fails the test unless want and got decode to the same JSON value; large numbers
// stay exact.
func AssertJSONEqual(t testing.TB, want, got []byte) {
	t.Helper()
	if !jsonassert.Equal(t, got, want) {
		t.Fatalf("JSON differs:\n got: %s\nwant: %s", got, want)
	}
}

// MemoryContent is a content store held in memory. Reads return copies and do not verify hashes, so
// a test can tamper with a row and leave the code under test to notice.
type MemoryContent map[content.ContentID]content.StoredContent

func (m MemoryContent) PutContent(kind content.ContentKind, payload json.RawMessage) (content.ContentID, error) {
	stored, err := content.CanonicalContent(kind, payload)
	if err != nil {
		return "", err
	}
	if previous, ok := m[stored.ID]; ok && !bytes.Equal(previous.Payload, stored.Payload) {
		return "", fmt.Errorf("content ID collision: %s", stored.ID)
	}
	m[stored.ID] = stored
	return stored.ID, nil
}

func (m MemoryContent) GetContent(id content.ContentID) (content.StoredContent, error) {
	stored, ok := m[id]
	if !ok {
		return content.StoredContent{}, fmt.Errorf("missing content %s", id)
	}
	stored.Payload = bytes.Clone(stored.Payload)
	return stored, nil
}
