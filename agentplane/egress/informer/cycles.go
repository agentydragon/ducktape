package informer

import (
	"io"
	"net/http"
	"path"
	"strconv"
	"time"

	"github.com/agentydragon/ducktape/agentplane/egress/policy"
)

// cycleRecorder wraps the API clients' transport so that a watch the server ends on schedule
// records a completed cycle of the kind it watched. A watch that ends early, on an error, or never
// is not a cycle, so a kind whose watch is wedged stops recording and goes stale.
type cycleRecorder struct {
	index *policy.Index
	now   func() time.Time
}

func (c cycleRecorder) wrap(next http.RoundTripper) http.RoundTripper {
	return &recordingTransport{next: next, cycleRecorder: c}
}

type recordingTransport struct {
	next http.RoundTripper
	cycleRecorder
}

// WrappedRoundTripper lets client-go see through to the transport it configured.
func (t *recordingTransport) WrappedRoundTripper() http.RoundTripper { return t.next }

func (t *recordingTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	started := t.now()
	response, err := t.next.RoundTrip(request)
	if err != nil || response.StatusCode != http.StatusOK {
		return response, err
	}
	query := request.URL.Query()
	period, err := strconv.ParseInt(query.Get("timeoutSeconds"), 10, 64)
	if request.Method != http.MethodGet || query.Get("watch") != "true" || err != nil {
		return response, nil
	}
	response.Body = &cycleBody{
		ReadCloser: response.Body, cycleRecorder: t.cycleRecorder,
		kind: policy.Kind(path.Base(request.URL.Path)), started: started, period: time.Duration(period) * time.Second,
	}
	return response, nil
}

// cycleBody is a watch's response stream; reading it to a clean end after the full period is the
// cycle.
type cycleBody struct {
	io.ReadCloser
	cycleRecorder
	kind    policy.Kind
	started time.Time
	period  time.Duration
}

func (b *cycleBody) Read(p []byte) (int, error) {
	n, err := b.ReadCloser.Read(p)
	if err == io.EOF {
		// The server's own timeout ends the stream cleanly; a dropped connection is an error, and a
		// server restart or eviction ends it cleanly but early.
		if ended := b.now(); ended.Sub(b.started) >= b.period {
			b.index.RecordCycle(b.kind, ended)
		}
	}
	return n, err
}
