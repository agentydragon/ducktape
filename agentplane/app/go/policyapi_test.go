package main

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/app/go/auth"
)

func TestPolicyRoutesArePublishedAndCallerProtected(t *testing.T) {
	authentication, err := auth.New(auth.Config{})
	if err != nil {
		t.Fatal(err)
	}
	server := NewHTTPServer(authentication)
	RegisterPolicyRoutes(server, nil, nil)
	handler := server.Handler(authentication)

	for _, path := range []string{
		"/sandboxes/form",
		"/sandboxes/demo/egress",
		"/sandboxes/demo/policy-bindings",
		"/policy-bindings/demo",
	} {
		method := http.MethodGet
		if path == "/sandboxes/demo/policy-bindings" {
			method = http.MethodPost
		} else if path == "/policy-bindings/demo" {
			method = http.MethodDelete
		}
		recorder := httptest.NewRecorder()
		handler.ServeHTTP(recorder, httptest.NewRequest(method, path, nil))
		if recorder.Code != http.StatusUnauthorized {
			t.Errorf("%s %s status = %d, want 401; body: %s", method, path, recorder.Code, recorder.Body.String())
		}
	}

	openapi := httptest.NewRecorder()
	handler.ServeHTTP(openapi, httptest.NewRequest(http.MethodGet, "/openapi.json", nil))
	if openapi.Code != http.StatusOK {
		t.Fatalf("GET /openapi.json status = %d, want 200; body: %s", openapi.Code, openapi.Body.String())
	}
}
