package gateway

import (
	"encoding/pem"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestProxyAuthorizesRoutesAndFiltersInternalBoundary(t *testing.T) {
	calls := 0
	provider := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.EscapedPath() != "/internal/proxy/personal/conversations/abc" || r.URL.RawQuery != "limit=1" {
			t.Errorf("wrong target %s", r.URL.String())
		}
		if r.Header.Get("Authorization") != "Bearer "+strings.Repeat("r", 40) || r.Header.Get("Cookie") != "" || r.Header.Get("X-Strip") != "" {
			t.Errorf("incorrect forwarded headers")
		}
		w.Header().Set(resultHeader, "1")
		w.Header().Set("Set-Cookie", "secret=x")
		w.Header().Set("Location", "https://upstream.invalid/api/conversations/def?q=1")
		w.WriteHeader(http.StatusFound)
	}))
	defer provider.Close()
	ca := filepath.Join(t.TempDir(), "ca.pem")
	if err := os.WriteFile(ca, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: provider.Certificate().Raw}), 0600); err != nil {
		t.Fatal(err)
	}
	bearer := strings.Repeat("b", 40)
	gateway := &proxy{providers: map[string]ProviderHTTPConfig{"personal": {ID: "personal", Origin: provider.URL, Upstream: "https://upstream.invalid/api", OwnerToken: strings.Repeat("o", 40), RouteToken: strings.Repeat("r", 40), CAFile: ca, RequestHeaders: []string{"x-strip"}}}, bearers: []Bearer{{Token: bearer, Routes: []Route{{Instance: "personal", Method: "GET", Path: "/conversations/*"}}}}}
	request := httptest.NewRequest("GET", "https://gateway.invalid/proxy/personal/conversations/abc?limit=1", nil)
	request.Header.Set("Authorization", "Bearer "+bearer)
	request.Header.Set("Cookie", "secret")
	request.Header.Set("X-Strip", "private")
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
	ca := filepath.Join(t.TempDir(), "ca.pem")
	if err := os.WriteFile(ca, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw}), 0600); err != nil {
		t.Fatal(err)
	}
	token := strings.Repeat("x", 40)
	gateway := &proxy{providers: map[string]ProviderHTTPConfig{"p": {Origin: server.URL, Upstream: "https://upstream.invalid", OwnerToken: token, CAFile: ca}}, bearers: []Bearer{{Token: token, Owner: true}}}
	req := httptest.NewRequest("GET", "https://gateway.invalid/proxy/p/x", nil)
	req.Header.Set("Authorization", "Bearer "+token)
	out := httptest.NewRecorder()
	gateway.ServeHTTP(out, req)
	if out.Code != 502 || strings.Contains(out.Body.String(), "untrusted") {
		t.Fatalf("untrusted response: %d %q", out.Code, out.Body.String())
	}
}
