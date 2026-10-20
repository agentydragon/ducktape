// Package agentdocs checks the documentation an agent API serves.
package agentdocs

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"net/url"
	"regexp"
	"strings"
	"testing"
)

// links finds the Markdown links of a document.
var links = regexp.MustCompile(`\[[^\]]+\]\(([^)]+)\)`)

// AssertServed follows the links of handler's /llms.txt that point into /docs/agents/ or at /openapi.json, and
// fails t for each document that is not served as it should be: a guide is a text document that opens with a
// heading, and the OpenAPI document is JSON.
func AssertServed(t *testing.T, handler http.Handler) {
	t.Helper()
	pending, visited := []string{"/llms.txt"}, map[string]bool{}
	for len(pending) > 0 {
		path := pending[0]
		pending = pending[1:]
		if visited[path] {
			continue
		}
		visited[path] = true
		response := httptest.NewRecorder()
		handler.ServeHTTP(response, httptest.NewRequest(http.MethodGet, path, nil))
		if strings.HasSuffix(path, ".json") {
			if response.Code != http.StatusOK || response.Header().Get("Content-Type") != "application/json" || !json.Valid(response.Body.Bytes()) {
				t.Errorf("GET %s = %d %q %.20q, want a served JSON document", path, response.Code, response.Header().Get("Content-Type"), response.Body)
			}
		} else if response.Code != http.StatusOK || !strings.HasPrefix(response.Header().Get("Content-Type"), "text/") ||
			!strings.HasPrefix(response.Body.String(), "# ") {
			t.Errorf("GET %s = %d %q %.20q, want a served text document that opens with a heading", path, response.Code, response.Header().Get("Content-Type"), response.Body)
		}
		for _, match := range links.FindAllStringSubmatch(response.Body.String(), -1) {
			target, err := url.Parse(match[1])
			if err == nil && target.Host == "" && (strings.HasPrefix(target.Path, "/docs/agents/") || target.Path == "/openapi.json") {
				pending = append(pending, target.Path)
			}
		}
	}
	if len(visited) < 2 {
		t.Errorf("visited %v, want llms.txt and the documents it links", visited)
	}
}
