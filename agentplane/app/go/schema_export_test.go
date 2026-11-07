package main

import (
	"bytes"
	"encoding/json"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestHTTPServerServesHumaOpenAPIDocument(t *testing.T) {
	server := NewHTTPServer(nil)
	RegisterPolicyRoutes(server, Config{}, nil, nil)
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

func TestExportOpenAPIExcludesMigratedSystemStatusRoute(t *testing.T) {
	var output bytes.Buffer
	if err := exportOpenAPI(&output); err != nil {
		t.Fatal(err)
	}
	document := assertSaneOpenAPI(t, output.Bytes())
	if _, ok := document.Paths["/system/status"]; ok {
		t.Fatal("Huma OpenAPI document still includes the migrated GET /system/status route")
	}
}

func TestExportOpenAPISandboxFormSchema(t *testing.T) {
	var output bytes.Buffer
	if err := exportOpenAPI(&output); err != nil {
		t.Fatal(err)
	}
	document := assertSaneOpenAPI(t, output.Bytes())
	for schemaName, fields := range map[string][]string{
		"SandboxFormOutputBody": {"policy_sets"},
	} {
		schema := document.Components.Schemas[schemaName]
		if schema == nil {
			t.Fatalf("OpenAPI document omits %s", schemaName)
		}
		properties, ok := schema["properties"].(map[string]any)
		if !ok {
			t.Fatalf("%s.properties = %T, want object", schemaName, schema["properties"])
		}
		for _, field := range fields {
			property, ok := properties[field].(map[string]any)
			if !ok || property["type"] != "array" {
				t.Errorf("%s.%s = %v, want a non-null array", schemaName, field, properties[field])
			}
		}
	}
	form := document.Components.Schemas["SandboxFormOutputBody"]
	formProperties, ok := form["properties"].(map[string]any)
	if !ok {
		t.Fatalf("SandboxFormOutputBody.properties = %T, want object", form["properties"])
	}
	defaults, ok := formProperties["new_sandbox_defaults"].(map[string]any)
	if !ok || defaults["$ref"] != "#/components/schemas/NewSandboxDefaults" {
		t.Fatalf("SandboxFormOutputBody.new_sandbox_defaults = %v, want NewSandboxDefaults reference", formProperties["new_sandbox_defaults"])
	}
	defaultsSchema := document.Components.Schemas["NewSandboxDefaults"]
	defaultsProperties, ok := defaultsSchema["properties"].(map[string]any)
	if !ok || defaultsProperties["wake_on_send"] == nil || defaultsProperties["idle_suspend_after_seconds"] == nil {
		t.Fatalf("NewSandboxDefaults.properties = %v, want wake and idle defaults", defaultsSchema["properties"])
	}
}

func TestPolicyGrantOpenAPIReturnsNoBody(t *testing.T) {
	var output bytes.Buffer
	if err := exportOpenAPI(&output); err != nil {
		t.Fatal(err)
	}
	document := assertSaneOpenAPI(t, output.Bytes())
	var operation struct {
		Responses map[string]struct {
			Content map[string]json.RawMessage `json:"content"`
		} `json:"responses"`
	}
	if err := json.Unmarshal(document.Paths["/sandboxes/{name}/policy-bindings"]["post"], &operation); err != nil {
		t.Fatal(err)
	}
	created, ok := operation.Responses["201"]
	if !ok || len(created.Content) != 0 {
		t.Fatalf("grant response = %#v, want 201 without a body", operation.Responses)
	}
}

type openAPIDocument struct {
	OpenAPI string `json:"openapi"`
	Info    struct {
		Title   string `json:"title"`
		Version string `json:"version"`
	} `json:"info"`
	Paths      map[string]map[string]json.RawMessage `json:"paths"`
	Components struct {
		Schemas map[string]map[string]any `json:"schemas"`
	} `json:"components"`
}

func assertSaneOpenAPI(t *testing.T, encoded []byte) openAPIDocument {
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
	if len(document.Paths) == 0 {
		t.Fatal("OpenAPI document has no paths")
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
	return document
}
