package gateway

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	providerpb "github.com/agentydragon/ducktape/platform_sessions/provider/v1/providerpb"
)

func testOwnerConfig() OwnerConfig {
	return OwnerConfig{Issuer: "https://issuer.invalid/", ClientID: "client", ClientSecret: strings.Repeat("c", 40), SessionSecret: strings.Repeat("s", 40), PublicBaseURL: "https://sessions.invalid", AllowedSubject: "owner"}
}

func TestGeneratedHTTPRequiresOwnerAndSameOriginForBrowserMutations(t *testing.T) {
	service, err := New(nil)
	if err != nil {
		t.Fatal(err)
	}
	bearer := strings.Repeat("o", 40)
	handler, err := HTTPServer(context.Background(), service, HTTPConfig{Owner: testOwnerConfig(), Bearers: []Bearer{{Token: bearer, Owner: true}}, Assets: map[string][]byte{"status.html": []byte("<body>test</body>")}})
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest("GET", "https://sessions.invalid/api/v1/instances", nil)
	result := httptest.NewRecorder()
	handler.ServeHTTP(result, request)
	if result.Code != 401 {
		t.Fatalf("unauthenticated: %d", result.Code)
	}
	request = httptest.NewRequest("GET", "https://sessions.invalid/api/v1/instances", nil)
	request.Header.Set("Authorization", "Bearer "+bearer)
	result = httptest.NewRecorder()
	handler.ServeHTTP(result, request)
	if result.Code != 200 || !strings.Contains(result.Body.String(), "instances") {
		t.Fatalf("authorized: %d %s", result.Code, result.Body)
	}
	request = httptest.NewRequest("POST", "https://sessions.invalid/api/v1/instances/nope:probe", strings.NewReader("{}"))
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
	request = httptest.NewRequest("GET", "https://sessions.invalid/api/v1/instances", nil)
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
	service, err := New([]Provider{{Ref: &providerpb.InstanceRef{ProviderId: "chatgpt", InstanceId: "personal"}, Client: fake}})
	if err != nil {
		t.Fatal(err)
	}
	ownerToken := strings.Repeat("o", 40)
	handler, err := HTTPServer(context.Background(), service, HTTPConfig{
		Owner: testOwnerConfig(), Bearers: []Bearer{{Token: ownerToken, Owner: true}},
		Providers: map[string]ProviderHTTPConfig{"personal": {ID: "personal", Origin: "https://provider.invalid", OwnerToken: strings.Repeat("i", 40), RouteToken: strings.Repeat("r", 40)}},
	})
	if err != nil {
		t.Fatal(err)
	}
	request := httptest.NewRequest("POST", "https://sessions.invalid/api/v1/instances/personal:importSession", strings.NewReader(`{"mediaType":"application/json","formatVersion":"1","candidate":"dGVzdA=="}`))
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Authorization", "Bearer "+ownerToken)
	result := httptest.NewRecorder()
	handler.ServeHTTP(result, request)
	if result.Code != 200 || fake.calls != 1 || !strings.Contains(result.Body.String(), "IMPORT_OUTCOME_INSTALLED") {
		t.Fatalf("import: status=%d body=%s calls=%d", result.Code, result.Body, fake.calls)
	}
}
