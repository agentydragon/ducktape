package gateway

import (
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func TestProxyAuthorizesRoutesAndFiltersInternalBoundary(t *testing.T) {
	calls := 0
	provider := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.EscapedPath() != "/internal/proxy/personal/conversations/abc" || r.URL.RawQuery != "limit=1" {
			t.Errorf("wrong target %s", r.URL.String())
		}
		if r.Header.Get("Authorization") != "Bearer "+strings.Repeat("r", 40) || r.Header.Get("Cookie") != "" {
			t.Errorf("incorrect forwarded headers")
		}
		w.Header().Set(resultHeader, "1")
		w.Header().Set("Set-Cookie", "secret=x")
		w.Header().Set("Location", "/conversations/def?q=1")
		w.WriteHeader(http.StatusFound)
	}))
	defer provider.Close()
	ca := trustedCAFile(t, provider)
	bearer := strings.Repeat("b", 40)
	gateway := &proxy{providers: map[string]ProviderHTTPConfig{"personal": {ID: "personal", Origin: provider.URL, OwnerToken: strings.Repeat("o", 40), RouteToken: strings.Repeat("r", 40), CAFile: ca, OperationTimeout: time.Second}}, bearers: []Bearer{{Token: bearer, Routes: []Route{{Instance: "personal", Method: "GET", Path: "/conversations/*"}}}}}
	request := httptest.NewRequest("GET", "https://gateway.invalid/proxy/personal/conversations/abc?limit=1", nil)
	request.Header.Set("Authorization", "Bearer "+bearer)
	request.Header.Set("Cookie", "secret")
	response := httptest.NewRecorder()
	gateway.ServeHTTP(response, request)
	if response.Code != 302 || response.Header().Get("Location") != "/proxy/personal/conversations/def?q=1" || response.Header().Get("Set-Cookie") != "" || response.Header().Get(resultHeader) != "" || calls != 1 {
		t.Fatalf("response: %d headers=%v calls=%d", response.Code, response.Header(), calls)
	}
	bad := httptest.NewRequest("POST", "https://gateway.invalid/proxy/personal/conversations/abc", nil)
	bad.Header.Set("Authorization", "Bearer "+bearer)
	response = httptest.NewRecorder()
	gateway.ServeHTTP(response, bad)
	if response.Code != 403 || calls != 1 {
		t.Fatalf("unauthorized method: %d calls=%d", response.Code, calls)
	}
	bad = httptest.NewRequest("GET", "https://gateway.invalid/proxy/personal/conversations/%252Fsecret", nil)
	bad.Header.Set("Authorization", "Bearer "+bearer)
	response = httptest.NewRecorder()
	gateway.ServeHTTP(response, bad)
	if response.Code != 400 || calls != 1 {
		t.Fatalf("encoded delimiter: %d calls=%d", response.Code, calls)
	}
}

func TestProxyRejectsUnmarkedProviderResponse(t *testing.T) {
	server := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { _, _ = io.WriteString(w, "untrusted") }))
	defer server.Close()
	ca := trustedCAFile(t, server)
	token := strings.Repeat("x", 40)
	gateway := &proxy{providers: map[string]ProviderHTTPConfig{"p": {Origin: server.URL, OwnerToken: token, CAFile: ca, OperationTimeout: time.Second}}, bearers: []Bearer{{Token: token, Owner: true}}}
	req := httptest.NewRequest("GET", "https://gateway.invalid/proxy/p/x", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	out := httptest.NewRecorder()
	gateway.ServeHTTP(out, req)
	if out.Code != 502 || strings.Contains(out.Body.String(), "untrusted") {
		t.Fatalf("untrusted response: %d %q", out.Code, out.Body.String())
	}
}

func TestProxyOperationDeadlineCancelsProviderRequest(t *testing.T) {
	started := make(chan struct{})
	canceled := make(chan struct{})
	var calls atomic.Int32
	provider := httptest.NewTLSServer(http.HandlerFunc(func(_ http.ResponseWriter, r *http.Request) {
		calls.Add(1)
		close(started)
		<-r.Context().Done()
		close(canceled)
	}))
	defer provider.Close()
	ca := trustedCAFile(t, provider)
	token := strings.Repeat("x", 40)
	// Leave enough time for the TLS handshake before testing cancellation of the handler.
	gateway := &proxy{providers: map[string]ProviderHTTPConfig{"p": {Origin: provider.URL, OwnerToken: token, CAFile: ca, OperationTimeout: time.Second}}, bearers: []Bearer{{Token: token, Owner: true}}}
	req := httptest.NewRequest("GET", "https://gateway.invalid/proxy/p/x", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	out := httptest.NewRecorder()
	done := make(chan struct{})
	go func() {
		gateway.ServeHTTP(out, req)
		close(done)
	}()
	select {
	case <-started:
	case <-time.After(time.Second):
		t.Fatal("provider request was not dispatched")
	}
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("gateway did not apply provider operation deadline")
	}
	select {
	case <-canceled:
	case <-time.After(time.Second):
		t.Fatal("provider request context was not canceled")
	}
	if calls.Load() != 1 || out.Code != http.StatusBadGateway {
		t.Fatalf("response status=%d provider dispatches=%d", out.Code, calls.Load())
	}
}

func TestSegmentsRejectAmbiguousPaths(t *testing.T) {
	for _, path := range []string{"/a/%2F", "/a/%5c", "/a/%252f", "/a/%2e%2e", "/a//b", "/a/%00", "/a/%ff", "/a/%", "/a/has?query"} {
		if _, err := segments(path, false); err == nil {
			t.Errorf("accepted ambiguous path %q", path)
		}
	}
	for _, path := range []string{"/a/%2a", "/a/pre*", "/a/**"} {
		if _, err := segments(path, true); err == nil {
			t.Errorf("accepted ambiguous wildcard %q", path)
		}
	}
	if _, err := segments("/a/*", true); err != nil {
		t.Fatalf("single segment wildcard: %v", err)
	}
}

func TestProviderRedirectMustBeAbsolutePath(t *testing.T) {
	for _, location := range []string{"https://evil.invalid/a", "//evil.invalid/a", "/a/%2fescape", "relative"} {
		if _, err := rewriteLocation(location, "id"); err == nil {
			t.Errorf("accepted redirect %q", location)
		}
	}
	got, err := rewriteLocation("/new?x=1", "id")
	if err != nil || got != "/proxy/id/new?x=1" {
		t.Fatalf("provider redirect: %q %v", got, err)
	}
}

func TestConnectionNominatedHeadersAreRemoved(t *testing.T) {
	headers := http.Header{"Connection": []string{"X-Custom, keep-alive"}, "X-Custom": []string{"secret"}, "X-Visible": []string{"ok"}, "Cookie": []string{"secret"}}
	filtered := filterHeaders(headers, requestPrivate)
	if filtered.Get("X-Custom") != "" || filtered.Get("Cookie") != "" || filtered.Get("Connection") != "" || filtered.Get("X-Visible") != "ok" {
		t.Fatalf("headers: %v", filtered)
	}
}
