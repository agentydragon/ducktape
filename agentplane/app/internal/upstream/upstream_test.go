package upstream

import (
	"context"
	"io"
	"maps"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"
)

func TestForwardHTTPForwardsOnlyAllowlistedHeaders(t *testing.T) {
	var forwarded *http.Request
	upstream := NewHandlerUpstream(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) { forwarded = r }), "")
	request := httptest.NewRequest(http.MethodPatch, "/upstream/v1/items/42?dry_run=true", nil)
	request.Header.Set("Accept", "application/json")
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("User-Agent", strings.Repeat("x", 301))
	request.Header.Set("Cookie", "browser-cookie")
	request.Header.Set("Authorization", "browser-credential")
	request.Header.Set("X-Secret", "secret")
	if err := upstream.ForwardHTTP(httptest.NewRecorder(), request, request.Context(), "v1/items/42", "operator-token"); err != nil {
		t.Fatal(err)
	}
	if forwarded.Method != http.MethodPatch || forwarded.URL.Path != "/v1/items/42" || forwarded.URL.RawQuery != "dry_run=true" {
		t.Errorf("forwarded request = %s %s?%s", forwarded.Method, forwarded.URL.Path, forwarded.URL.RawQuery)
	}
	want := http.Header{
		"Accept": {"application/json"}, "Content-Type": {"application/json"}, "User-Agent": {strings.Repeat("x", 300)},
		"Authorization": {"Bearer operator-token"}, "Accept-Encoding": {"identity"},
	}
	if !reflect.DeepEqual(forwarded.Header, want) {
		t.Errorf("forwarded headers = %v, want %v", forwarded.Header, want)
	}
}

func TestGeneratedFacadeRouteAllowed(t *testing.T) {
	for _, test := range []struct {
		method string
		path   string
		want   bool
	}{
		{http.MethodGet, "v1/items", true},
		{http.MethodPost, "v1/items:search", true},
		{http.MethodPatch, "v1/items/42", true},
		{http.MethodDelete, "v1/items/42", true},
		{http.MethodPut, "v1/items/42", false},
		{http.MethodPost, "v2/items", false},
		{http.MethodPost, "v1/../admin", false},
		{http.MethodPost, "v1//items", false},
		{http.MethodPost, `v1/items\admin`, false},
	} {
		t.Run(test.method+"_"+test.path, func(t *testing.T) {
			if got := GeneratedFacadeRouteAllowed(test.method, test.path); got != test.want {
				t.Fatalf("GeneratedFacadeRouteAllowed(%q, %q) = %v, want %v", test.method, test.path, got, test.want)
			}
		})
	}
}

func TestRelayFlushesSmallLiveUpdatesAndCancelsUpstreamOnDisconnect(t *testing.T) {
	for _, contentType := range []string{"text/event-stream", "application/x-ndjson"} {
		t.Run(contentType, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			emit := make(chan string)
			upstreamClosed := make(chan struct{})
			upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				defer close(upstreamClosed)
				w.Header().Set("Content-Type", contentType)
				w.WriteHeader(http.StatusOK)
				w.(http.Flusher).Flush()
				for {
					select {
					case <-r.Context().Done():
						return
					case data := <-emit:
						_, _ = io.WriteString(w, data)
						w.(http.Flusher).Flush()
					}
				}
			}))
			defer upstream.Close()
			proxy, err := NewUpstreamProxy(upstream.Client(), upstream.URL)
			if err != nil {
				t.Fatal(err)
			}
			relay := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				proxy.Relay(w, r, RelayRequest{
					Method: http.MethodGet, Path: "watch", Header: http.Header{},
					ResponseHeaders: map[string]struct{}{"content-type": {}}, CacheControl: "no-store",
					OnError: func(http.ResponseWriter, *http.Request, error) { t.Error("relay failed") },
				})
			}))
			defer relay.Close()
			request, err := http.NewRequestWithContext(ctx, http.MethodGet, relay.URL, nil)
			if err != nil {
				t.Fatal(err)
			}
			response, err := relay.Client().Do(request)
			if err != nil {
				t.Fatalf("headers did not arrive before the first update: %v", err)
			}
			defer response.Body.Close()
			for _, update := range []string{"data: first\n\n", "data: second\n\n"} {
				select {
				case emit <- update:
				case <-ctx.Done():
					t.Fatal(ctx.Err())
				}
				body := make([]byte, len(update))
				if _, err := io.ReadFull(response.Body, body); err != nil {
					t.Fatalf("small update was buffered until a later write: %v", err)
				}
				if string(body) != update {
					t.Fatalf("update = %q, want %q", body, update)
				}
			}
			if err := response.Body.Close(); err != nil {
				t.Fatal(err)
			}
			select {
			case <-upstreamClosed:
			case <-ctx.Done():
				t.Fatal("browser disconnect did not close the upstream watch")
			}
		})
	}
}

func TestRelayExposesOnlyAllowlistedHeadersAndForcesCacheControl(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Link", "</early>; rel=preload")
		w.WriteHeader(http.StatusEarlyHints)
		w.Header().Del("Link")
		w.Header().Add("Vary", "Accept")
		w.Header().Add("Vary", "Origin")
		w.Header().Set("ETag", `"shape"`)
		w.Header().Set("Content-Type", "application/json")
		w.Header().Set("Cache-Control", "public, max-age=3600")
		w.Header().Set("Set-Cookie", "upstream=session")
		w.Header().Set("Location", "https://internal.invalid/")
		w.Header().Set("X-Internal", "secret")
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Trailer", "X-Trailer-Secret")
		w.WriteHeader(http.StatusOK)
		_, _ = io.WriteString(w, "body")
		w.Header().Set("X-Trailer-Secret", "secret")
	}))
	defer upstream.Close()
	proxy, err := NewUpstreamProxy(upstream.Client(), upstream.URL)
	if err != nil {
		t.Fatal(err)
	}
	relay := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		proxy.Relay(w, r, RelayRequest{
			Method: http.MethodGet, Path: "v1/x", Header: http.Header{},
			ResponseHeaders: map[string]struct{}{"content-type": {}, "etag": {}, "vary": {}, "cache-control": {}},
			CacheControl:    "private, no-cache",
			OnError:         func(http.ResponseWriter, *http.Request, error) { t.Error("relay failed") },
		})
	}))
	defer relay.Close()
	response, err := relay.Client().Get(relay.URL)
	if err != nil {
		t.Fatal(err)
	}
	defer response.Body.Close()
	if body, _ := io.ReadAll(response.Body); string(body) != "body" {
		t.Fatalf("body = %q", body)
	}
	got := make(map[string]string)
	for name, values := range response.Header {
		got[name] = strings.Join(values, ",")
	}
	// Date and Transfer-Encoding are set by this server, not relayed.
	delete(got, "Date")
	delete(got, "Content-Length")
	want := map[string]string{
		"Content-Type": "application/json", "Etag": `"shape"`, "Vary": "Accept", "Cache-Control": "private, no-cache",
	}
	if !maps.Equal(got, want) {
		t.Fatalf("response headers = %v, want %v", got, want)
	}
	if len(response.Trailer) != 0 {
		t.Fatalf("upstream trailers leaked: %v", response.Trailer)
	}
}

func TestRelayMapsUpstreamFailureToErrorHandler(t *testing.T) {
	closed := httptest.NewServer(http.NotFoundHandler())
	proxy, err := NewUpstreamProxy(closed.Client(), closed.URL)
	if err != nil {
		t.Fatal(err)
	}
	closed.Close()
	var relayErr error
	recorder := httptest.NewRecorder()
	proxy.Relay(recorder, httptest.NewRequest(http.MethodGet, "/", nil), RelayRequest{
		Method: http.MethodGet, Path: "v1/x", Header: http.Header{},
		OnError: func(w http.ResponseWriter, _ *http.Request, err error) {
			relayErr = err
			w.WriteHeader(http.StatusServiceUnavailable)
		},
	})
	if relayErr == nil || recorder.Code != http.StatusServiceUnavailable {
		t.Fatalf("error=%v status=%d", relayErr, recorder.Code)
	}
}

func TestRelayForwardsOnlyDeclaredRequest(t *testing.T) {
	upstream := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		if r.Method != http.MethodPost || r.URL.Path != "/v1/x" || r.URL.RawQuery != "a=b" || string(body) != "payload" {
			t.Errorf("request = %s %s?%s %q", r.Method, r.URL.Path, r.URL.RawQuery, body)
		}
		if r.Header.Get("Cookie") != "" || r.Header.Get("Authorization") != "" || r.Header.Get("X-Forwarded-For") != "" {
			t.Errorf("inbound headers leaked: %v", r.Header)
		}
		if r.Header.Get("X-Declared") != "yes" || r.Header.Get("Accept-Encoding") != "identity" {
			t.Errorf("declared headers = %v", r.Header)
		}
	}))
	defer upstream.Close()
	proxy, err := NewUpstreamProxy(upstream.Client(), upstream.URL)
	if err != nil {
		t.Fatal(err)
	}
	inbound := httptest.NewRequest(http.MethodGet, "/ignored?c=d", strings.NewReader("ignored"))
	inbound.Header.Set("Cookie", "browser")
	inbound.Header.Set("Authorization", "browser")
	inbound.Header.Set("Accept-Encoding", "gzip")
	proxy.Relay(httptest.NewRecorder(), inbound, RelayRequest{
		Method: http.MethodPost, Path: "v1/x", Query: "a=b", Body: []byte("payload"),
		Header:  http.Header{"X-Declared": {"yes"}},
		OnError: func(http.ResponseWriter, *http.Request, error) { t.Error("relay failed") },
	})
}
