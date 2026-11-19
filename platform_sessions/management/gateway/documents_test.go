package gateway

import (
	"context"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	providerpb "github.com/agentydragon/ducktape/platform_sessions/provider/providerpb"
)

func TestProviderDocumentComesFromAuthenticatedProvider(t *testing.T) {
	ownerToken := strings.Repeat("o", 40)
	calls := 0
	providerHTTP := httptest.NewTLSServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		calls++
		if r.URL.Path != "/internal/openapi/personal" || r.Header.Get("Authorization") != "Bearer "+ownerToken {
			t.Error("incorrect provider document request")
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"openapi":"3.1.0","servers":[{"url":"https://wrong.invalid"}],"paths":{}}`))
	}))
	defer providerHTTP.Close()
	ca := trustedCAFile(t, providerHTTP)
	fake := &fakeProvider{documentPath: "/internal/openapi/personal"}
	service, err := New([]Provider{{Ref: &providerpb.InstanceRef{ProviderId: "chatgpt", InstanceId: "personal"}, Client: fake}})
	if err != nil {
		t.Fatal(err)
	}
	routeToken := strings.Repeat("r", 40)
	consumer := strings.Repeat("b", 40)
	handler, err := HTTPServer(context.Background(), service, HTTPConfig{Owner: testOwnerConfig(), Bearers: []Bearer{{Token: consumer, Routes: []Route{{Instance: "personal", Method: "GET", Path: "/x"}}}}, Providers: map[string]ProviderHTTPConfig{"personal": {ID: "personal", Origin: providerHTTP.URL, OwnerToken: ownerToken, RouteToken: routeToken, CAFile: ca, OperationTimeout: time.Minute}}})
	if err != nil {
		t.Fatal(err)
	}
	req := httptest.NewRequest("GET", "https://sessions.invalid/api/integrations/personal/openapi.json", nil)
	out := httptest.NewRecorder()
	handler.ServeHTTP(out, req)
	if out.Code != 401 || calls != 0 {
		t.Fatalf("unauthenticated: %d calls=%d", out.Code, calls)
	}
	req.Header.Set("Authorization", "Bearer "+consumer)
	out = httptest.NewRecorder()
	handler.ServeHTTP(out, req)
	if out.Code != 200 || calls != 1 || !strings.Contains(out.Body.String(), `/proxy/personal`) || strings.Contains(out.Body.String(), "wrong.invalid") {
		t.Fatalf("document: %d %s calls=%d", out.Code, out.Body, calls)
	}
}
