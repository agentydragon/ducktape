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
	routes, err := newRoutes(http.NotFoundHandler(), nil)
	if err != nil {
		t.Fatal(err)
	}
	agentdocs.AssertServed(t, routes)
}

// The OpenAPI document is the only description of the routes that a harness without the command has, and the
// guides send agents to these.
func TestTheOpenAPIDocumentDescribesTheRoutesTheGuidesUse(t *testing.T) {
	routes, err := newRoutes(http.NotFoundHandler(), nil)
	if err != nil {
		t.Fatal(err)
	}
	served := httptest.NewRecorder()
	routes.ServeHTTP(served, httptest.NewRequest(http.MethodGet, "/openapi.json", nil))
	var document struct {
		Paths       map[string]map[string]json.RawMessage `json:"paths"`
		Definitions map[string]struct {
			Properties map[string]json.RawMessage `json:"properties"`
		} `json:"definitions"`
	}
	if err := json.Unmarshal(served.Body.Bytes(), &document); err != nil {
		t.Fatal(err)
	}

	for _, route := range []struct{ method, path, suffix string }{
		{"post", "/v1/subscriptions", ""},
		{"get", "/v1/subscriptions", ""},
		{"patch", "/v1/subscriptions/", ""},
		{"get", "/v1/inboxes/", "/entries"},
		{"put", "/v1/inboxes/", "/acknowledgement"},
	} {
		found := false
		for path, operations := range document.Paths {
			if _, described := operations[route.method]; described && strings.HasPrefix(path, route.path) && strings.HasSuffix(path, route.suffix) {
				found = true
			}
		}
		if !found {
			t.Errorf("no %s %s...%s in the OpenAPI document", route.method, route.path, route.suffix)
		}
	}
	for name, definition := range document.Definitions {
		if _, paused := definition.Properties["paused"]; paused {
			t.Errorf("%s has a `paused` property: a subscription is renewed, never paused", name)
		}
	}
}
