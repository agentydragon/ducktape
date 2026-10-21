package main

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/testing/agentdocs"
)

// The documentation needs neither the database nor a peer, so an agent can read it before anything else is up.
func TestAgentDocumentationIsServedWithoutTheBackend(t *testing.T) {
	routes, err := newRoutes(http.NotFoundHandler())
	if err != nil {
		t.Fatal(err)
	}
	agentdocs.AssertServed(t, routes)
}

// The OpenAPI document is the only description of the routes that a harness without the command has; the HTTP
// reference sends agents to it for the reads this service answers.
func TestTheOpenAPIDocumentDescribesTheReadsTheGuidesUse(t *testing.T) {
	routes, err := newRoutes(http.NotFoundHandler())
	if err != nil {
		t.Fatal(err)
	}
	served := httptest.NewRecorder()
	routes.ServeHTTP(served, httptest.NewRequest(http.MethodGet, "/openapi.json", nil))
	var document struct {
		Paths map[string]map[string]json.RawMessage `json:"paths"`
	}
	if err := json.Unmarshal(served.Body.Bytes(), &document); err != nil {
		t.Fatal(err)
	}
	for _, route := range []struct{ prefix, suffix string }{
		{"/v1/action-requests", "/action-requests"},
		{"/v1/action-requests/{", "}"},
		{"/v1/action-requests/{", "}/events"},
	} {
		found := false
		for path, operations := range document.Paths {
			if _, described := operations["get"]; described && strings.HasPrefix(path, route.prefix) && strings.HasSuffix(path, route.suffix) {
				found = true
			}
		}
		if !found {
			t.Errorf("no GET %s...%s in the OpenAPI document", route.prefix, route.suffix)
		}
	}
}
