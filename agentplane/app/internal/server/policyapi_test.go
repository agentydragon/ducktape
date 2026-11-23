package server

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/app/internal/auth"
	streamhttp "github.com/agentydragon/ducktape/agentplane/app/internal/httpapi/streams"
)

func TestPolicyRoutesArePublishedAndCallerProtected(t *testing.T) {
	authentication, err := auth.New(auth.Config{})
	if err != nil {
		t.Fatal(err)
	}
	server := newHTTPServer(authentication)
	streamhttp.RegisterRoutes(server, server.DrainDone(), streamhttp.Dependencies{})
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
}
