package quicktype_test

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"testing"

	"github.com/santhosh-tekuri/jsonschema/v6"

	"github.com/agentydragon/ducktape/devinfra/x/schema_codegen/infrastructure/quicktype"
)

// widget.schema.json is the source fixture shared with quicktype's generator.
//
//go:embed widget.schema.json
var widgetSchemaJSON []byte

func TestGeneratedWidgetConsumesJSON(t *testing.T) {
	var got models.Widget
	if err := json.Unmarshal([]byte(`{"name":"demo","replicas":2,"labels":["blue"]}`), &got); err != nil {
		t.Fatal(err)
	}
	if got.Name != "demo" || got.Replicas != 2 || len(got.Labels) != 1 || got.Labels[0] != "blue" {
		t.Fatalf("decoded widget = %#v", got)
	}
}

func TestGeneratedWidgetRejectsWrongJSONFieldType(t *testing.T) {
	var got models.Widget
	if err := json.Unmarshal([]byte(`{"name":"demo","replicas":"two","labels":[]}`), &got); err == nil {
		t.Fatal("json.Unmarshal accepted a string for the generated integer field")
	}
}

func TestSchemaValidationIsSeparateFromModelDecoding(t *testing.T) {
	const validBySchema = `{"name":"demo","replicas":2,"labels":[]}`
	const invalidBySchema = `{"name":"demo","replicas":0,"labels":[]}`
	var model models.Widget
	if err := json.Unmarshal([]byte(invalidBySchema), &model); err != nil {
		t.Fatalf("quicktype model rejected JSON shape: %v", err)
	}

	validator := compileWidgetSchema(t)
	valid, err := jsonschema.UnmarshalJSON(bytes.NewReader([]byte(validBySchema)))
	if err != nil {
		t.Fatal(err)
	}
	if err := validator.Validate(valid); err != nil {
		t.Fatalf("JSON Schema rejected valid fixture: %v", err)
	}

	decoded, err := jsonschema.UnmarshalJSON(bytes.NewReader([]byte(invalidBySchema)))
	if err != nil {
		t.Fatal(err)
	}
	if err := validator.Validate(decoded); err == nil {
		t.Fatal("JSON Schema accepted replicas below its minimum")
	}
}

func compileWidgetSchema(t *testing.T) *jsonschema.Schema {
	t.Helper()
	document, err := jsonschema.UnmarshalJSON(bytes.NewReader(widgetSchemaJSON))
	if err != nil {
		t.Fatal(err)
	}
	compiler := jsonschema.NewCompiler()
	compiler.DefaultDraft(jsonschema.Draft2020)
	const schemaURL = "https://example.test/widget.schema.json"
	if err := compiler.AddResource(schemaURL, document); err != nil {
		t.Fatal(err)
	}
	compiled, err := compiler.Compile(schemaURL)
	if err != nil {
		t.Fatal(err)
	}
	return compiled
}
