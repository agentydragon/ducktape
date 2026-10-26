package proxy

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

type fakeThreadScopeStore struct {
	mu       sync.Mutex
	exists   bool
	scope    ThreadScope
	hasScope bool
	changes  chan struct{}
}

func newFakeThreadScopeStore(exists bool, scope ThreadScope, hasScope bool) *fakeThreadScopeStore {
	return &fakeThreadScopeStore{exists: exists, scope: scope, hasScope: hasScope, changes: make(chan struct{}, 1)}
}

func (s *fakeThreadScopeStore) ThreadExists(context.Context, string) (bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.exists, nil
}

func (s *fakeThreadScopeStore) CurrentScope(context.Context, string) (ThreadScope, bool, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.scope, s.hasScope, nil
}

func (s *fakeThreadScopeStore) SubscribeThreadChanges(context.Context, string) (<-chan struct{}, func()) {
	return s.changes, func() {}
}

func (s *fakeThreadScopeStore) setScope(scope ThreadScope) {
	s.mu.Lock()
	s.scope, s.hasScope = scope, true
	s.mu.Unlock()
	s.changes <- struct{}{}
}

func mustElectricProxy(t *testing.T, client *http.Client, serverURL string, store ThreadScopeStore, hold time.Duration) *ElectricProxy {
	t.Helper()
	proxy, err := NewElectricProxy(client, serverURL, store, hold)
	if err != nil {
		t.Fatal(err)
	}
	return proxy
}

func TestElectricEntityForwardPinsThreadAndStreamsFilteredResponse(t *testing.T) {
	const threadID = "00000000-0000-0000-0000-000000000001"
	const epoch = "projection-7"
	var seenQuery url.Values
	var seenHeaders http.Header
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seenQuery = r.URL.Query()
		seenHeaders = r.Header.Clone()
		if r.Method != http.MethodGet || r.URL.Path != "/v1/shape" {
			t.Errorf("upstream request = %s %s", r.Method, r.URL.Path)
		}
		w.Header().Set("Electric-Offset", "7_0")
		w.Header().Set("Electric-Up-To-Date", "true")
		w.Header().Set("ETag", `"shape"`)
		w.Header().Set("X-Private", "drop")
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusPartialContent)
		_, _ = io.WriteString(w, "streamed electric body")
	}))
	defer server.Close()
	store := newFakeThreadScopeStore(true, ThreadScope{ProjectionEpoch: epoch, ThroughCursor: "42"}, true)
	mux := http.NewServeMux()
	mustElectricProxy(t, server.Client(), server.URL, store, time.Second).Register(mux)

	request := httptest.NewRequest(http.MethodGet,
		"/threads/"+threadID+"/sync/entities?projection_epoch="+epoch+"&offset=now&handle=h&live=true&live_sse=true&experimental_live_sse=true&cursor=c&expired_handle=old&cache-buster=b&log=changes_only",
		nil)
	request.Header.Set("Accept", "application/json")
	request.Header.Set("Electric-Protocol-Version", "1.0")
	request.Header.Set("Cookie", "browser-session")
	request.Header.Set("Authorization", "browser-credential")
	response := httptest.NewRecorder()
	mux.ServeHTTP(response, request)

	if response.Code != http.StatusPartialContent || response.Body.String() != "streamed electric body" {
		t.Fatalf("response = %d %q", response.Code, response.Body.String())
	}
	for key, want := range map[string]string{
		"Electric-Offset": "7_0", "Electric-Up-To-Date": "true", "ETag": `"shape"`,
		"Cache-Control": "private, no-cache", "Content-Type": "application/json",
	} {
		if got := response.Header().Get(key); got != want {
			t.Errorf("response %s = %q, want %q", key, got, want)
		}
	}
	if response.Header().Get("X-Private") != "" {
		t.Error("private upstream header was forwarded")
	}
	for key, want := range map[string]string{
		"table": "thread_entity", "columns": entityColumns, "where": entityWhere,
		"replica": "full", "log": "changes_only", "queryable_columns": entityColumns,
		"params[1]": threadID, "params[2]": epoch,
	} {
		if got := seenQuery.Get(key); got != want {
			t.Errorf("upstream query %s = %q, want %q", key, got, want)
		}
	}
	for key, want := range map[string]string{
		"offset": "now", "handle": "h", "live": "true", "live_sse": "true",
		"experimental_live_sse": "true", "cursor": "c", "expired_handle": "old", "cache-buster": "b",
	} {
		if got := seenQuery.Get(key); got != want {
			t.Errorf("passthrough query %s = %q, want %q", key, got, want)
		}
	}
	if seenQuery.Has("projection_epoch") {
		t.Error("app-only projection_epoch was forwarded")
	}
	if seenHeaders.Get("Electric-Protocol-Version") != "1.0" || seenHeaders.Get("Accept") != "application/json" {
		t.Errorf("allowed Electric request headers missing: %v", seenHeaders)
	}
	if seenHeaders.Get("Cookie") != "" || seenHeaders.Get("Authorization") != "" {
		t.Errorf("browser credentials crossed Electric proxy: %v", seenHeaders)
	}
	if seenHeaders.Get("Accept-Encoding") != "identity" {
		t.Errorf("Accept-Encoding = %q", seenHeaders.Get("Accept-Encoding"))
	}
}

func TestElectricSubsetIsValidatedAndForwarded(t *testing.T) {
	const threadID = "00000000-0000-0000-0000-000000000002"
	const epoch = "epoch"
	var forwarded SubsetRequest
	var forwardedQuery url.Values
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			t.Errorf("method = %s", r.Method)
		}
		forwardedQuery = r.URL.Query()
		if err := json.NewDecoder(r.Body).Decode(&forwarded); err != nil {
			t.Errorf("decode forwarded subset: %v", err)
		}
		w.WriteHeader(http.StatusNotModified)
	}))
	defer server.Close()
	store := newFakeThreadScopeStore(true, ThreadScope{ProjectionEpoch: epoch}, true)
	mux := http.NewServeMux()
	mustElectricProxy(t, server.Client(), server.URL, store, time.Second).Register(mux)
	subsetJSON := `{"where":"entity_index < $1","params":{"1":"40"},"order_by":"entity_index DESC","limit":200}`
	request := httptest.NewRequest(http.MethodPost,
		"/threads/"+threadID+"/sync/entities?projection_epoch="+epoch+"&offset=0_0&handle=h",
		strings.NewReader(subsetJSON))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("If-None-Match", `"etag"`)
	response := httptest.NewRecorder()
	mux.ServeHTTP(response, request)
	if response.Code != http.StatusNotModified || response.Body.Len() != 0 {
		t.Fatalf("response = %d %q", response.Code, response.Body.String())
	}
	if forwarded.Where == nil || *forwarded.Where != "entity_index < $1" || forwarded.Params["1"] != "40" ||
		forwarded.OrderBy == nil || *forwarded.OrderBy != entityOrder || forwarded.Limit == nil || *forwarded.Limit != 200 {
		t.Fatalf("forwarded subset = %+v", forwarded)
	}
	if forwardedQuery.Get("params[1]") != threadID || forwardedQuery.Get("params[2]") != epoch {
		t.Fatalf("pinned query parameters = %v", forwardedQuery)
	}
}

func TestElectricChunksPinsFieldAndAcceptsBoundedBodySubset(t *testing.T) {
	const threadID = "00000000-0000-0000-0000-000000000003"
	const epoch = "epoch"
	var query url.Values
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		query = r.URL.Query()
		_, _ = io.WriteString(w, "[]")
	}))
	defer server.Close()
	store := newFakeThreadScopeStore(true, ThreadScope{ProjectionEpoch: epoch}, true)
	mux := http.NewServeMux()
	mustElectricProxy(t, server.Client(), server.URL, store, time.Second).Register(mux)
	where := "(owner_id = $1 AND generation = $2) OR (owner_id = $3 AND generation = $4)"
	body := `{"where":"` + where + `","params":{"1":"item-a","2":"4","3":"item-b","4":"5"}}`
	request := httptest.NewRequest(http.MethodPost,
		"/threads/"+threadID+"/sync/chunks/confirmed_input?projection_epoch="+epoch+"&offset=0_0",
		strings.NewReader(body))
	response := httptest.NewRecorder()
	mux.ServeHTTP(response, request)
	if response.Code != http.StatusOK || response.Body.String() != "[]" {
		t.Fatalf("response = %d %q", response.Code, response.Body.String())
	}
	if query.Get("table") != "thread_payload_chunk" || query.Get("where") != chunkWhere || query.Get("params[3]") != "confirmed_input" {
		t.Fatalf("chunk shape was not pinned: %v", query)
	}
	if query.Get("params[1]") != threadID || query.Get("params[2]") != epoch {
		t.Fatalf("thread scope was not pinned: %v", query)
	}
}

func TestElectricScopeChecksThreadAndWaitsForFold(t *testing.T) {
	const threadID = "00000000-0000-0000-0000-000000000004"
	store := newFakeThreadScopeStore(true, ThreadScope{}, false)
	mux := http.NewServeMux()
	mustElectricProxy(t, nil, "https://electric.invalid", store, 300*time.Millisecond).Register(mux)
	request := httptest.NewRequest(http.MethodGet, "/threads/"+threadID+"/sync/scope", nil)
	response := httptest.NewRecorder()
	completed := make(chan struct{})
	go func() {
		mux.ServeHTTP(response, request)
		close(completed)
	}()
	time.Sleep(10 * time.Millisecond)
	store.setScope(ThreadScope{ProjectionEpoch: "epoch-2", ThroughCursor: "91"})
	select {
	case <-completed:
	case <-time.After(time.Second):
		t.Fatal("scope request did not wake after a thread change")
	}
	if response.Code != http.StatusOK {
		t.Fatalf("scope status = %d body=%q", response.Code, response.Body.String())
	}
	var got ThreadScope
	if err := json.Unmarshal(response.Body.Bytes(), &got); err != nil {
		t.Fatal(err)
	}
	if got != (ThreadScope{ProjectionEpoch: "epoch-2", ThroughCursor: "91"}) {
		t.Fatalf("scope = %+v", got)
	}

	missing := newFakeThreadScopeStore(false, ThreadScope{}, false)
	missingMux := http.NewServeMux()
	mustElectricProxy(t, nil, "https://electric.invalid", missing, time.Millisecond).Register(missingMux)
	missingResponse := httptest.NewRecorder()
	missingMux.ServeHTTP(missingResponse, httptest.NewRequest(http.MethodGet, "/threads/"+threadID+"/sync/scope", nil))
	if missingResponse.Code != http.StatusNotFound {
		t.Fatalf("missing thread status = %d", missingResponse.Code)
	}
}

func TestElectricScopeReturnsNoContentAfterHold(t *testing.T) {
	const threadID = "00000000-0000-0000-0000-000000000005"
	store := newFakeThreadScopeStore(true, ThreadScope{}, false)
	mux := http.NewServeMux()
	mustElectricProxy(t, nil, "https://electric.invalid", store, time.Millisecond).Register(mux)
	response := httptest.NewRecorder()
	mux.ServeHTTP(response, httptest.NewRequest(http.MethodGet, "/threads/"+threadID+"/sync/scope", nil))
	if response.Code != http.StatusNoContent || response.Body.Len() != 0 {
		t.Fatalf("scope response = %d %q", response.Code, response.Body.String())
	}
}

func TestElectricRejectsUnpinnedRequestsBeforeUpstream(t *testing.T) {
	const threadID = "00000000-0000-0000-0000-000000000006"
	const epoch = "epoch"
	var calls int
	server := httptest.NewServer(http.HandlerFunc(func(http.ResponseWriter, *http.Request) { calls++ }))
	defer server.Close()
	store := newFakeThreadScopeStore(true, ThreadScope{ProjectionEpoch: epoch}, true)
	mux := http.NewServeMux()
	mustElectricProxy(t, server.Client(), server.URL, store, time.Second).Register(mux)
	for _, test := range []struct {
		name string
		path string
		body string
		want int
	}{
		{"unknown query", "?projection_epoch=epoch&table=thread_entity", "", http.StatusBadRequest},
		{"duplicate offset", "?projection_epoch=epoch&offset=now&offset=old", "", http.StatusBadRequest},
		{"unsupported log", "?projection_epoch=epoch&log=full", "", http.StatusBadRequest},
		{"stale epoch", "?projection_epoch=retired", "", http.StatusGone},
		{"unknown subset field", "?projection_epoch=epoch", `{"offset":1}`, http.StatusUnprocessableEntity},
		{"unsafe entity subset", "?projection_epoch=epoch", `{"where":"true","order_by":"entity_index DESC","limit":1}`, http.StatusBadRequest},
		{"missing body param", "?projection_epoch=epoch", `{"where":"(owner_id = $1 AND generation = $2)","params":{"1":"x"}}`, http.StatusBadRequest},
	} {
		t.Run(test.name, func(t *testing.T) {
			method := http.MethodGet
			if test.body != "" {
				method = http.MethodPost
			}
			request := httptest.NewRequest(method, "/threads/"+threadID+"/sync/entities"+test.path, strings.NewReader(test.body))
			response := httptest.NewRecorder()
			mux.ServeHTTP(response, request)
			if response.Code != test.want {
				t.Fatalf("status = %d, body %q, want %d", response.Code, response.Body.String(), test.want)
			}
		})
	}
	if calls != 0 {
		t.Fatalf("invalid requests reached upstream %d times", calls)
	}
}

func TestSubsetLimitsAndRequestSize(t *testing.T) {
	for _, test := range []struct {
		name  string
		check func() error
		want  bool
	}{
		{"entity row maximum", func() error {
			return checkEntitySubset(SubsetRequest{OrderBy: stringPtr(entityOrder), Limit: intPtr(SubsetRowLimit)})
		}, true},
		{"entity row overflow", func() error {
			return checkEntitySubset(SubsetRequest{OrderBy: stringPtr(entityOrder), Limit: intPtr(SubsetRowLimit + 1)})
		}, false},
		{"body maximum", func() error { return checkBodySubset(bodySubset(SubsetBodyLimit)) }, true},
		{"body overflow", func() error { return checkBodySubset(bodySubset(SubsetBodyLimit + 1)) }, false},
	} {
		t.Run(test.name, func(t *testing.T) {
			if got := test.check() == nil; got != test.want {
				t.Fatalf("accepted = %v, want %v", got, test.want)
			}
		})
	}

	const threadID = "00000000-0000-0000-0000-000000000007"
	store := newFakeThreadScopeStore(true, ThreadScope{ProjectionEpoch: "epoch"}, true)
	mux := http.NewServeMux()
	mustElectricProxy(t, nil, "https://electric.invalid", store, time.Second).Register(mux)
	largeBody := `{"where":"` + strings.Repeat("x", MaxSubsetRequestBytes) + `"}`
	request := httptest.NewRequest(http.MethodPost,
		"/threads/"+threadID+"/sync/entities?projection_epoch=epoch", strings.NewReader(largeBody))
	response := httptest.NewRecorder()
	mux.ServeHTTP(response, request)
	if response.Code != http.StatusRequestEntityTooLarge {
		t.Fatalf("oversize request status = %d", response.Code)
	}
}

func bodySubset(count int) SubsetRequest {
	var where strings.Builder
	params := make(map[string]string, count*2)
	for index := 1; index <= count; index++ {
		if index > 1 {
			where.WriteString(" OR ")
		}
		fmt.Fprintf(&where, "(owner_id = $%d AND generation = $%d)", 2*index-1, 2*index)
		params[strconv.Itoa(2*index-1)] = "owner"
		params[strconv.Itoa(2*index)] = "1"
	}
	value := where.String()
	return SubsetRequest{Where: &value, Params: params}
}

func stringPtr(value string) *string { return &value }
func intPtr(value int) *int          { return &value }
