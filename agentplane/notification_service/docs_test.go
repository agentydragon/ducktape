package main

import (
	"net/http"
	"net/http/httptest"
	"testing"
)

func TestOnlyTheSignedWebhookHasAnHTTPRoute(t *testing.T) {
	routes := newRoutes(nil)
	for _, path := range []string{
		"/v1/subscriptions",
		"/v1/messages",
		"/llms.txt",
		"/docs/agents/subscriptions.md",
		"/openapi.json",
	} {
		t.Run(path, func(t *testing.T) {
			served := httptest.NewRecorder()
			routes.ServeHTTP(served, httptest.NewRequest(http.MethodGet, path, nil))
			if served.Code != http.StatusNotFound {
				t.Errorf("GET %s = %d, want 404", path, served.Code)
			}
		})
	}
}
