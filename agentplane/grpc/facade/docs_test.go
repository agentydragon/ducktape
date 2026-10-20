package facade

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"testing/fstest"
)

func TestAgentDocsServeFilesAndTheOpenAPIDocumentOnly(t *testing.T) {
	mux := http.NewServeMux()
	MountAgentDocs(mux, fstest.MapFS{
		"llms.txt":         {Data: []byte("# index")},
		"guides/start.md":  {Data: []byte("# start")},
		"credentials.md":   {Data: []byte("# credentials")},
		"credentials.json": {Data: []byte("{}")},
	}, []byte(`{"swagger":"2.0"}`))

	for path, want := range map[string]struct {
		code        int
		contentType string
	}{
		"/llms.txt":                     {http.StatusOK, "text/plain"},
		"/openapi.json":                 {http.StatusOK, "application/json"},
		"/docs/agents/credentials.md":   {http.StatusOK, "text/markdown"},
		"/docs/agents/credentials.json": {http.StatusOK, "text/plain"},
		"/docs/agents/guides/start.md":  {http.StatusOK, "text/markdown"},
		"/docs/agents/guides":           {http.StatusNotFound, ""},
		"/docs/agents/missing.md":       {http.StatusNotFound, ""},
	} {
		recorded := httptest.NewRecorder()
		mux.ServeHTTP(recorded, httptest.NewRequest(http.MethodGet, path, nil))
		if recorded.Code != want.code || (want.contentType != "" && !strings.HasPrefix(recorded.Header().Get("Content-Type"), want.contentType)) {
			t.Errorf("GET %s = %d %q, want %d %q", path, recorded.Code, recorded.Header().Get("Content-Type"), want.code, want.contentType)
		}
	}
}
