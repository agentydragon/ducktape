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
	if err := RegisterCoreRoutes(server, Config{}); err != nil {
		t.Fatal(err)
	}
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

func TestFrontendConfigOpenAPIDocumentsNullableTargets(t *testing.T) {
	var output bytes.Buffer
	if err := exportOpenAPI(&output); err != nil {
		t.Fatal(err)
	}
	document := assertSaneOpenAPI(t, output.Bytes())
	if _, ok := document.Paths["/frontend-config"]["get"]; !ok {
		t.Fatal("OpenAPI document omits GET /frontend-config")
	}
	config := document.Components.Schemas["FrontendConfig"]
	properties, ok := config["properties"].(map[string]any)
	if !ok {
		t.Fatalf("FrontendConfig.properties = %v, want object", config["properties"])
	}
	for name, target := range map[string]string{
		"trace_search": "TraceSearchSettings", "langfuse_search": "LangfuseSearchSettings",
	} {
		property, ok := properties[name].(map[string]any)
		if !ok {
			t.Fatalf("FrontendConfig.%s = %v, want schema", name, properties[name])
		}
		alternatives, ok := property["anyOf"].([]any)
		if !ok || len(alternatives) != 2 {
			t.Fatalf("FrontendConfig.%s = %v, want target or null", name, property)
		}
		ref, refOK := alternatives[0].(map[string]any)
		null, nullOK := alternatives[1].(map[string]any)
		if !refOK || ref["$ref"] != "#/components/schemas/"+target || !nullOK || null["type"] != "null" {
			t.Errorf("FrontendConfig.%s = %v, want %s or null", name, property, target)
		}
	}
}

func TestExportOpenAPIUsesHumaSchemasForThreadRoutes(t *testing.T) {
	var output bytes.Buffer
	if err := exportOpenAPI(&output); err != nil {
		t.Fatal(err)
	}
	document := assertSaneOpenAPI(t, output.Bytes())
	for _, path := range []string{"/threads/{thread_id}", "/threads/{thread_id}/archive", "/threads/{thread_id}/unarchive"} {
		if _, ok := document.Paths[path]; !ok {
			t.Errorf("OpenAPI document omits Huma route %q", path)
		}
	}

	threadView := document.Components.Schemas["ThreadView"]
	if threadView == nil {
		t.Fatal("OpenAPI document omits ThreadView")
	}
	properties, ok := threadView["properties"].(map[string]any)
	if !ok {
		t.Fatalf("ThreadView.properties = %T, want object", threadView["properties"])
	}
	id, ok := properties["id"].(map[string]any)
	if !ok || id["format"] != "uuid" {
		t.Fatal("ThreadView.id must be documented as a UUID")
	}
	sandboxUID, ok := properties["sandbox_uid"].(map[string]any)
	if !ok || sandboxUID["format"] != nil {
		t.Fatalf("ThreadView.sandbox_uid = %v, want an opaque nullable string", properties["sandbox_uid"])
	}
	types, ok := sandboxUID["type"].([]any)
	if !ok || len(types) != 2 || types[0] != "string" || types[1] != "null" {
		t.Fatalf("ThreadView.sandbox_uid.type = %v, want string or null", sandboxUID["type"])
	}
	lastCursor, ok := properties["last_cursor"].(map[string]any)
	if !ok || lastCursor["type"] != "integer" || lastCursor["format"] != "int64" {
		t.Fatal("ThreadView.last_cursor must be documented as an int64")
	}
	for schemaName, fields := range map[string][]string{
		"BindingView":           {"subjects", "conditions", "policy_sets", "missing_policy_sets"},
		"PolicySetView":         {"rules"},
		"CredentialView":        {"targets"},
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

func TestSandboxOpenAPIUsesUIDFencedDeleteAndRemovesDuplicateReadRoutes(t *testing.T) {
	var output bytes.Buffer
	if err := exportOpenAPI(&output); err != nil {
		t.Fatal(err)
	}
	document := assertSaneOpenAPI(t, output.Bytes())
	if _, ok := document.Paths["/sandboxes"]["get"]; ok {
		t.Fatal("app OpenAPI still exposes a duplicate Sandbox list route")
	}
	if _, ok := document.Paths["/sandboxes/{name}"]["get"]; ok {
		t.Fatal("app OpenAPI still exposes a duplicate Sandbox detail route")
	}
	var createOperation struct {
		Responses map[string]struct {
			Content map[string]struct {
				Schema map[string]any `json:"schema"`
			} `json:"content"`
		} `json:"responses"`
	}
	if err := json.Unmarshal(document.Paths["/sandboxes"]["post"], &createOperation); err != nil {
		t.Fatal(err)
	}
	acknowledgementHasTarget := false
	for _, response := range createOperation.Responses {
		if content, ok := response.Content["application/json"]; ok && schemaContainsProperties(document, content.Schema, "namespace", "name", "uid") {
			acknowledgementHasTarget = true
		}
	}
	if !acknowledgementHasTarget {
		t.Fatal("POST /sandboxes response schema must expose the exact created namespace, name, and UID")
	}
	var operation struct {
		RequestBody struct {
			Required bool `json:"required"`
			Content  map[string]struct {
				Schema map[string]any `json:"schema"`
			} `json:"content"`
		} `json:"requestBody"`
	}
	if err := json.Unmarshal(document.Paths["/sandboxes/{name}"]["delete"], &operation); err != nil {
		t.Fatal(err)
	}
	body, ok := operation.RequestBody.Content["application/json"]
	if !operation.RequestBody.Required || !ok {
		t.Fatal("DELETE /sandboxes/{name} must require a JSON UID body")
	}
	if !schemaContainsRequired(document, body.Schema, "uid") {
		t.Fatalf("DELETE request body schema = %v, want required uid", body.Schema)
	}
	if !schemaContainsProperties(document, body.Schema, "uid") {
		t.Fatalf("DELETE request body schema = %v, want uid property", body.Schema)
	}
}

func schemaContainsRequired(document openAPIDocument, schema map[string]any, name string) bool {
	if required, ok := schema["required"].([]any); ok {
		for _, candidate := range required {
			if candidate == name {
				return true
			}
		}
	}
	if reference, ok := schema["$ref"].(string); ok {
		const prefix = "#/components/schemas/"
		if strings.HasPrefix(reference, prefix) {
			if referenced := document.Components.Schemas[strings.TrimPrefix(reference, prefix)]; referenced != nil {
				return schemaContainsRequired(document, referenced, name)
			}
		}
	}
	for _, groupName := range []string{"allOf", "anyOf", "oneOf"} {
		if group, ok := schema[groupName].([]any); ok {
			for _, child := range group {
				if childSchema, ok := child.(map[string]any); ok && schemaContainsRequired(document, childSchema, name) {
					return true
				}
			}
		}
	}
	return false
}

func schemaContainsProperties(document openAPIDocument, schema map[string]any, names ...string) bool {
	if reference, ok := schema["$ref"].(string); ok {
		const prefix = "#/components/schemas/"
		if strings.HasPrefix(reference, prefix) {
			if referenced := document.Components.Schemas[strings.TrimPrefix(reference, prefix)]; referenced != nil {
				return schemaContainsProperties(document, referenced, names...)
			}
		}
	}
	if properties, ok := schema["properties"].(map[string]any); ok {
		allPresent := true
		for _, name := range names {
			if _, exists := properties[name]; !exists {
				allPresent = false
				break
			}
		}
		if allPresent {
			return true
		}
		for _, property := range properties {
			if child, ok := property.(map[string]any); ok && schemaContainsProperties(document, child, names...) {
				return true
			}
		}
	}
	for _, groupName := range []string{"allOf", "anyOf", "oneOf"} {
		if group, ok := schema[groupName].([]any); ok {
			for _, child := range group {
				if childSchema, ok := child.(map[string]any); ok && schemaContainsProperties(document, childSchema, names...) {
					return true
				}
			}
		}
	}
	return false
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
