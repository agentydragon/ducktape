package liveinventory

import (
	"io"
	"net/http"
	"net/url"
	"path"
	"strconv"
	"time"
)

// cycleRecorder sets only the watch lifetime and observes when client-go's watch stream ends
// cleanly after the server timeout. client-go continues to own every ListWatch/reconnect/relist.
type cycleRecorder struct{ index *Index }

func (c cycleRecorder) wrap(next http.RoundTripper) http.RoundTripper {
	return &recordingTransport{next: next, index: c.index}
}

type recordingTransport struct {
	next  http.RoundTripper
	index *Index
}

// WrappedRoundTripper lets client-go see through to its configured transport.
func (t *recordingTransport) WrappedRoundTripper() http.RoundTripper { return t.next }

func (t *recordingTransport) RoundTrip(request *http.Request) (*http.Response, error) {
	kind := kindByPath(request.URL)
	if request.Method != http.MethodGet || kind == "" {
		return t.next.RoundTrip(request)
	}
	query := request.URL.Query()
	if query.Get("watch") != "true" {
		response, err := t.next.RoundTrip(request)
		if err == nil && response != nil && response.StatusCode == http.StatusOK {
			t.index.recordList(kind)
		}
		return response, err
	}
	started := t.index.now()
	request = withWatchPeriod(request, t.index.period)
	response, err := t.next.RoundTrip(request)
	if err != nil || response == nil || response.StatusCode != http.StatusOK {
		return response, err
	}
	listRun := t.index.listRun(kind)
	if request.URL.Query().Get("sendInitialEvents") == "true" {
		listRun = t.index.recordList(kind)
	}
	response.Body = &cycleBody{
		ReadCloser: response.Body,
		index:      t.index,
		kind:       kind,
		listRun:    listRun,
		started:    started,
		period:     t.index.period,
	}
	return response, nil
}

func withWatchPeriod(request *http.Request, period time.Duration) *http.Request {
	copy := request.Clone(request.Context())
	urlCopy := *request.URL
	query := urlCopy.Query()
	query.Set("timeoutSeconds", strconv.FormatInt(int64(period/time.Second), 10))
	urlCopy.RawQuery = query.Encode()
	copy.URL = &urlCopy
	return copy
}

func kindByPath(watched *url.URL) string {
	kind := path.Base(watched.Path)
	for _, expected := range watchedKinds {
		if kind == expected || (expected == PolicyBindingsKind && kind == "policybindings") ||
			(expected == PolicySetsKind && kind == "policysets") {
			return expected
		}
	}
	return ""
}

type cycleBody struct {
	io.ReadCloser
	index   *Index
	kind    string
	listRun uint64
	started time.Time
	period  time.Duration
	ended   bool
}

func (b *cycleBody) Read(buffer []byte) (int, error) {
	n, err := b.ReadCloser.Read(buffer)
	if err != nil && !b.ended {
		b.ended = true
		if err == io.EOF {
			if ended := b.index.now(); ended.Sub(b.started) >= b.period {
				b.index.recordCycle(b.kind, b.listRun, ended)
			}
		}
	}
	return n, err
}

func (i *Index) recordList(kind string) uint64 {
	i.mu.Lock()
	defer i.mu.Unlock()
	i.listRuns[kind]++
	return i.listRuns[kind]
}

func (i *Index) listRun(kind string) uint64 {
	i.mu.RLock()
	defer i.mu.RUnlock()
	return i.listRuns[kind]
}

func (i *Index) recordCycle(kind string, listRun uint64, ended time.Time) {
	i.mu.Lock()
	if listRun > i.cycleRuns[kind] {
		i.cycles[kind] = ended
		i.cycleRuns[kind] = listRun
	}
	i.mu.Unlock()
}
