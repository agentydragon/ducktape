package main

import (
	"bytes"
	"encoding/json"
	"net/http/httptest"
	"reflect"
	"testing"
)

func TestHTTPServerServesCapturedOpenAPIContract(t *testing.T) {
	server := NewHTTPServer(nil)
	recorder := httptest.NewRecorder()
	server.Handler(nil).ServeHTTP(recorder, httptest.NewRequest("GET", "/openapi.json", nil))
	if recorder.Code != 200 {
		t.Fatalf("GET /openapi.json status = %d, want 200", recorder.Code)
	}
	if got := recorder.Header().Get("Content-Type"); got != "application/json" {
		t.Fatalf("GET /openapi.json content type = %q, want application/json", got)
	}
	if !bytes.Equal(recorder.Body.Bytes(), openapiCompatibilityJSON) {
		t.Fatal("GET /openapi.json differs from the captured public contract")
	}
}

func TestExportOpenAPIPublishesThreadRoutesAndSyncContract(t *testing.T) {
	var output bytes.Buffer
	if err := exportOpenAPI(&output); err != nil {
		t.Fatal(err)
	}
	var document struct {
		Paths      map[string]map[string]json.RawMessage `json:"paths"`
		Components struct {
			Schemas map[string]map[string]any `json:"schemas"`
		} `json:"components"`
	}
	if err := json.Unmarshal(output.Bytes(), &document); err != nil {
		t.Fatalf("decode exported schema: %v", err)
	}
	for _, path := range []string{"/threads/form", "/threads", "/threads/{thread_id}", "/threads/{thread_id}/evidence"} {
		if _, ok := document.Paths[path]; !ok {
			t.Errorf("OpenAPI document omits Huma route %q", path)
		}
	}

	scope := document.Components.Schemas["ThreadScopeResponse"]
	if scope == nil {
		t.Fatal("OpenAPI document omits ThreadScopeResponse")
	}
	properties := scope["properties"].(map[string]any)
	if properties["through_cursor"].(map[string]any)["type"] != "string" {
		t.Fatal("ThreadScopeResponse.through_cursor must remain a decimal string")
	}

	entity := document.Components.Schemas["ThreadEntityView"]
	if entity == nil {
		t.Fatal("OpenAPI document omits ThreadEntityView")
	}
	variants, ok := entity["oneOf"].([]any)
	if !ok || len(variants) != 5 {
		t.Fatalf("ThreadEntityView.oneOf = %v, want five entity variants", entity["oneOf"])
	}
	discriminator := entity["discriminator"].(map[string]any)
	if discriminator["propertyName"] != "entity_kind" {
		t.Fatalf("ThreadEntityView discriminator = %v, want entity_kind", discriminator["propertyName"])
	}
	mapping := discriminator["mapping"].(map[string]any)
	for _, kind := range []string{"view_state", "item", "confirmed_input", "lifecycle", "command"} {
		if mapping[kind] == nil {
			t.Errorf("ThreadEntityView discriminator has no %q mapping", kind)
		}
	}
	variant := document.Components.Schemas["ThreadViewStateEntityView"]
	variantProperties := variant["properties"].(map[string]any)
	if variantProperties["cursor"].(map[string]any)["type"] != "integer" {
		t.Fatal("ThreadEntityView cursor must remain an integer in the JSON contract")
	}
	if variantProperties["text_ref"].(map[string]any)["anyOf"] == nil {
		t.Fatal("ThreadEntityView text_ref must remain nullable")
	}
}

func TestExportOpenAPIPreservesPythonPublicContract(t *testing.T) {
	var output bytes.Buffer
	if err := exportOpenAPI(&output); err != nil {
		t.Fatal(err)
	}
	var document struct {
		Paths      map[string]map[string]json.RawMessage `json:"paths"`
		Components struct {
			Schemas map[string]map[string]any `json:"schemas"`
		} `json:"components"`
	}
	if err := json.Unmarshal(output.Bytes(), &document); err != nil {
		t.Fatalf("decode exported schema: %v", err)
	}
	var compatibility struct {
		Paths      map[string]json.RawMessage `json:"paths"`
		Components struct {
			Schemas map[string]json.RawMessage `json:"schemas"`
		} `json:"components"`
	}
	if err := json.Unmarshal(openapiCompatibilityJSON, &compatibility); err != nil {
		t.Fatalf("decode compatibility fragments: %v", err)
	}
	if got, want := len(document.Paths), 46; got != want {
		t.Fatalf("exported OpenAPI path count = %d, want %d", got, want)
	}
	if got, want := len(document.Components.Schemas), 99; got != want {
		t.Fatalf("exported OpenAPI schema count = %d, want %d", got, want)
	}
	for path, expectedRaw := range compatibility.Paths {
		actualRaw, ok := document.Paths[path]
		if !ok {
			t.Errorf("exported OpenAPI omits compatibility path %q", path)
			continue
		}
		actualJSON, err := json.Marshal(actualRaw)
		if err != nil {
			t.Errorf("encode exported path %q: %v", path, err)
			continue
		}
		var actual, expected any
		if err := json.Unmarshal(actualJSON, &actual); err != nil {
			t.Errorf("decode exported path %q: %v", path, err)
			continue
		}
		if err := json.Unmarshal(expectedRaw, &expected); err != nil {
			t.Errorf("decode compatibility path %q: %v", path, err)
			continue
		}
		if !reflect.DeepEqual(actual, expected) {
			t.Errorf("exported path %q differs from its compatibility contract", path)
		}
	}
	for name, expectedRaw := range compatibility.Components.Schemas {
		actual, ok := document.Components.Schemas[name]
		if !ok {
			t.Errorf("exported OpenAPI omits compatibility schema %q", name)
			continue
		}
		var expected map[string]any
		if err := json.Unmarshal(expectedRaw, &expected); err != nil {
			t.Errorf("decode compatibility schema %q: %v", name, err)
			continue
		}
		if !reflect.DeepEqual(actual, expected) {
			t.Errorf("exported schema %q differs from its compatibility contract", name)
		}
	}

	var callback struct {
		Get struct {
			Responses map[string]json.RawMessage `json:"responses"`
		} `json:"get"`
	}
	callbackPath, ok := document.Paths["/mcp-linkage/callback"]
	if !ok {
		t.Fatal("MCP callback route is missing from exported OpenAPI")
	}
	if err := json.Unmarshal(callbackPath["get"], &callback.Get); err != nil {
		t.Fatal(err)
	}
	if _, ok := callback.Get.Responses["303"]; !ok {
		t.Fatal("MCP callback OpenAPI must document its redirect response")
	}
	if _, ok := callback.Get.Responses["200"]; ok {
		t.Fatal("MCP callback OpenAPI must not document the former inferred 200 response")
	}
}
