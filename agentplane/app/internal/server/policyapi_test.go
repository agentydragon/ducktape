package server

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/app/internal/auth"
)

func TestPolicyRoutesArePublishedAndCallerProtected(t *testing.T) {
	authentication, err := auth.New(auth.Config{})
	if err != nil {
		t.Fatal(err)
	}
	server := newHTTPServer(authentication)
	registerStreamingRoutes(server, streamingDependencies{})
	handler := server.Handler(authentication)

	for _, request := range []struct{ method, path string }{
		{http.MethodPost, "/upstream/policy/v1/sandboxes/policies/demo/policy-bindings"},
		{http.MethodDelete, "/upstream/policy/v1/sandboxes/policies/demo/policy-bindings/grant?sandboxUid=sandbox-uid&bindingUid=binding-uid"},
	} {
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, httptest.NewRequest(request.method, request.path, nil))
		if recorder.Code != http.StatusUnauthorized {
			t.Errorf("%s %s status = %d, want 401; body: %s", request.method, request.path, recorder.Code, recorder.Body.String())
		}
	}

	openapi := httptest.NewRecorder()
	handler.ServeHTTP(openapi, httptest.NewRequest(http.MethodGet, "/openapi.json", nil))
	if openapi.Code != http.StatusOK {
		t.Fatalf("GET /openapi.json status = %d, want 200; body: %s", openapi.Code, openapi.Body.String())
	}
}
