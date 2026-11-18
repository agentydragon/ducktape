package server

import (
	"bytes"
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestHTTPServerServesHumaOpenAPIDocument(t *testing.T) {
	server := newHTTPServer(nil)
	recorder := httptest.NewRecorder()
	server.Handler(nil).ServeHTTP(recorder, httptest.NewRequest("GET", "/openapi.json", nil))
	if recorder.Code != 200 {
		t.Fatalf("GET /openapi.json status = %d, want 200", recorder.Code)
	}
	if got := recorder.Header().Get("Content-Type"); got != "application/openapi+json" {
		t.Fatalf("GET /openapi.json content type = %q, want application/openapi+json", got)
	}
	assertSaneOpenAPI(t, recorder.Body.Bytes())
}

func TestExportOpenAPIWritesSaneDocument(t *testing.T) {
	var output bytes.Buffer
	if err := ExportOpenAPI(&output); err != nil {
		t.Fatal(err)
	}
	assertSaneOpenAPI(t, output.Bytes())
}

type openAPIDocument struct {
	OpenAPI string `json:"openapi"`
	Info    struct {
		Title   string `json:"title"`
		Version string `json:"version"`
	} `json:"info"`
	Paths map[string]map[string]json.RawMessage `json:"paths"`
}

func assertSaneOpenAPI(t *testing.T, encoded []byte) {
	t.Helper()
	var document openAPIDocument
	if err := json.Unmarshal(encoded, &document); err != nil {
		t.Fatalf("decode OpenAPI document: %v", err)
	}
	if document.OpenAPI != "3.1.0" {
		t.Errorf("OpenAPI version = %q, want 3.1.0", document.OpenAPI)
	}
	if document.Info.Title == "" || document.Info.Version == "" {
		t.Errorf("OpenAPI info must have a title and version, got %+v", document.Info)
	}
	for path, pathItem := range document.Paths {
		for method, rawOperation := range pathItem {
			switch method {
			case "get", "post", "put", "patch", "delete", "head", "options", "trace":
			default:
				continue
			}
			var operation struct {
				OperationID string                     `json:"operationId"`
				Responses   map[string]json.RawMessage `json:"responses"`
				Parameters  []struct {
					In       string `json:"in"`
					Name     string `json:"name"`
					Required bool   `json:"required"`
				} `json:"parameters"`
			}
			if err := json.Unmarshal(rawOperation, &operation); err != nil {
				t.Errorf("decode %s %s operation: %v", method, path, err)
				continue
			}
			if operation.OperationID == "" {
				t.Errorf("%s %s has no operationId", method, path)
			}
			if len(operation.Responses) == 0 {
				t.Errorf("%s %s has no documented responses", method, path)
			}
			for rest := path; ; {
				open := strings.IndexByte(rest, '{')
				if open < 0 {
					break
				}
				close := strings.IndexByte(rest[open:], '}')
				if close < 0 {
					t.Errorf("path %q has an unclosed template parameter", path)
					break
				}
				name := rest[open+1 : open+close]
				found := false
				for _, parameter := range operation.Parameters {
					if parameter.In == "path" && parameter.Name == name && parameter.Required {
						found = true
						break
					}
				}
				if !found {
					t.Errorf("%s %s does not document required path parameter %q", method, path, name)
				}
				rest = rest[open+close+1:]
			}
		}
	}
}
