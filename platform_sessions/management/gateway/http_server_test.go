package gateway

import (
	"context"
	"encoding/pem"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

// trustedCAFile writes a TLS test server's certificate to a PEM file for `ProviderHTTPConfig.CAFile`.
func trustedCAFile(t *testing.T, server *httptest.Server) string {
	t.Helper()
	path := filepath.Join(t.TempDir(), "ca.pem")
	if err := os.WriteFile(path, pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: server.Certificate().Raw}), 0600); err != nil {
		t.Fatal(err)
	}
	return path
}

func testOwnerConfig() OwnerConfig {
	return OwnerConfig{Issuer: "https://issuer.invalid/", ClientID: "client", ClientSecret: strings.Repeat("c", 40), SessionSecret: strings.Repeat("s", 40), PublicBaseURL: "https://sessions.invalid", AllowedSubject: "owner"}
}

func TestGeneratedHTTPRequiresOwnerAndSameOriginForBrowserMutations(t *testing.T) {
	service, err := New(nil)
	if err != nil {
		t.Fatal(err)
	}
	bearer := strings.Repeat("o", 40)
	handler, err := HTTPServer(context.Background(), service, HTTPConfig{
		Owner:   testOwnerConfig(),
		Bearers: []Bearer{{Token: bearer, Owner: true}},
		Assets: map[string][]byte{
			"status.html": []byte("<body>test</body>"),
			"main.css":    []byte("body { color: red; }"),
			"main.js":     []byte("console.log('test');"),
		},
	})
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest("GET", "https://sessions.invalid/main.js", nil)
	result := httptest.NewRecorder()
	handler.ServeHTTP(result, request)
	if result.Code != http.StatusSeeOther || result.Header().Get("Location") != "/auth/login" {
		t.Fatalf("unauthenticated script: status=%d location=%q", result.Code, result.Header().Get("Location"))
	}
	request = httptest.NewRequest("GET", "https://sessions.invalid/api/instances", nil)
	result = httptest.NewRecorder()
	handler.ServeHTTP(result, request)
	if result.Code != 401 {
		t.Fatalf("unauthenticated: %d", result.Code)
	}
	request = httptest.NewRequest("GET", "https://sessions.invalid/api/instances", nil)
	request.Header.Set("Authorization", "Bearer "+bearer)
	result = httptest.NewRecorder()
	handler.ServeHTTP(result, request)
	if result.Code != 200 || !strings.Contains(result.Body.String(), "instances") {
		t.Fatalf("authorized: %d %s", result.Code, result.Body)
	}
	request = httptest.NewRequest("POST", "https://sessions.invalid/api/instances/nope:probe", strings.NewReader("{}"))
	request.Header.Set("Authorization", "Bearer "+bearer)
	request.Header.Set("Origin", "https://evil.invalid")
	result = httptest.NewRecorder()
	handler.ServeHTTP(result, request)
	if result.Code != 403 {
		t.Fatalf("cross origin: %d", result.Code)
	}
	owner, err := newOwnerAuth(testOwnerConfig())
	if err != nil {
		t.Fatal(err)
	}
	good, err := owner.cookie.Encode(ownerCookie, ownerSession{Issuer: owner.cfg.Issuer, Subject: "owner", Username: "user", ExpiresAt: 1 << 62})
	if err != nil {
		t.Fatal(err)
	}
	request = httptest.NewRequest("GET", "https://sessions.invalid/", nil)
	request.AddCookie(&http.Cookie{Name: ownerCookie, Value: good})
	result = httptest.NewRecorder()
	handler.ServeHTTP(result, request)
	if result.Code != 200 || result.Body.String() != "<body>test</body>" {
		t.Fatalf("owner UI: %d %q", result.Code, result.Body)
	}
	const csp = "default-src 'none'; script-src 'self'; connect-src 'self' wss://sessions.invalid; style-src 'self'; img-src data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
	if result.Header().Get("Content-Security-Policy") != csp {
		t.Fatalf("owner UI CSP: %q", result.Header().Get("Content-Security-Policy"))
	}
	for _, asset := range []struct {
		path, contentType, body string
	}{
		{"main.css", "text/css", "body { color: red; }"},
		{"main.js", "application/javascript", "console.log('test');"},
	} {
		request = httptest.NewRequest("GET", "https://sessions.invalid/"+asset.path, nil)
		request.AddCookie(&http.Cookie{Name: ownerCookie, Value: good})
		result = httptest.NewRecorder()
		handler.ServeHTTP(result, request)
		if result.Code != http.StatusOK || result.Header().Get("Content-Type") != asset.contentType || result.Body.String() != asset.body {
			t.Fatalf("owner UI asset %s: status=%d content-type=%q body=%q", asset.path, result.Code, result.Header().Get("Content-Type"), result.Body.String())
		}
	}
	request = httptest.NewRequest("GET", "https://sessions.invalid/api/instances", nil)
	request.AddCookie(&http.Cookie{Name: ownerCookie, Value: good})
	request.Header.Set("Authorization", "Bearer bad")
	result = httptest.NewRecorder()
	handler.ServeHTTP(result, request)
	if result.Code != 403 {
		t.Fatalf("bad explicit bearer with cookie: %d", result.Code)
	}
}

func TestHTTPImportReachesProviderOnce(t *testing.T) {
	fake := &fakeProvider{}
	service := newTestService(t, fake)
	ownerToken := strings.Repeat("o", 40)
	handler, err := HTTPServer(context.Background(), service, HTTPConfig{
		Owner: testOwnerConfig(), Bearers: []Bearer{{Token: ownerToken, Owner: true}},
		Providers: map[string]ProviderHTTPConfig{"personal": {ID: "personal", Origin: "https://provider.invalid", OwnerToken: strings.Repeat("i", 40), RouteToken: strings.Repeat("r", 40), OperationTimeout: time.Minute}},
	})
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest("POST", "https://sessions.invalid/api/instances/personal:importSession", strings.NewReader(`{"mediaType":"application/json","candidate":"dGVzdA=="}`))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+ownerToken)
	result := httptest.NewRecorder()
	handler.ServeHTTP(result, request)
	if result.Code != 200 || fake.calls != 1 || !strings.Contains(result.Body.String(), `"installed":{}`) {
		t.Fatalf("import: status=%d body=%s calls=%d", result.Code, result.Body, fake.calls)
	}
}
