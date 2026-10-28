package catalog

import (
	"encoding/json"
	"errors"
	"testing"

	"github.com/invopop/jsonschema"
)

type inputPlace struct {
	Name string `json:"name" jsonschema:"minLength=1"`
}

type testInput struct {
	Place inputPlace `json:"place"`
	Other inputPlace `json:"other"`
	Cwd   *string    `json:"cwd,omitempty" jsonschema:"nullable"`
	Label *string    `json:"label,omitempty"`
	Count int        `json:"count" jsonschema:"minimum=1,maximum=100,default=3"`
}

func TestInputAdvertisementAndTypedDecode(t *testing.T) {
	input, err := NewInput[testInput](InputOptions{Name: "run", Description: "Run a task"})
	if err != nil {
		t.Fatal(err)
	}
	var schema map[string]any
	if err := json.Unmarshal(input.Definition().InputSchema, &schema); err != nil {
		t.Fatal(err)
	}
	if schema["type"] != "object" || schema["additionalProperties"] != false {
		t.Fatalf("root is not closed object: %v", schema)
	}
	definitions, ok := schema["$defs"].(map[string]any)
	if !ok || len(definitions) != 1 {
		t.Fatalf("expected one reusable local definition: %v", schema["$defs"])
	}
	place, ok := definitions["inputPlace"].(map[string]any)
	if !ok || place["additionalProperties"] != false {
		t.Fatalf("nested object is not closed: %v", place)
	}
	properties := schema["properties"].(map[string]any)
	if properties["place"].(map[string]any)["$ref"] != "#/$defs/inputPlace" ||
		properties["other"].(map[string]any)["$ref"] != "#/$defs/inputPlace" {
		t.Fatalf("reused fields do not refer to local definition: %v", properties)
	}
	if properties["count"].(map[string]any)["type"] != "integer" ||
		properties["count"].(map[string]any)["default"] != float64(3) {
		t.Fatalf("integer reflection or numeric default changed: %v", properties["count"])
	}
	for _, payload := range []string{
		`{"place":{"name":"a"},"other":{"name":"b"},"count":60}`,
		`{"place":{"name":"a"},"other":{"name":"b"},"count":60,"cwd":null}`,
		`{"place":{"name":"a"},"other":{"name":"b"},"count":60,"cwd":"/tmp"}`,
	} {
		decoded, err := input.Decode(json.RawMessage(payload))
		if err != nil || decoded.Count != 60 || decoded.Place.Name != "a" || decoded.Other.Name != "b" {
			t.Errorf("Decode(%s) = %+v, %v", payload, decoded, err)
		}
	}
	for _, payload := range []string{
		`{"other":{"name":"b"},"count":60}`,
		`{"place":{"name":"a"},"other":{"name":"b"}}`,
		`{"place":{"name":"a","extra":true},"other":{"name":"b"},"count":60}`,
		`{"place":{"name":"a"},"other":{"name":"b"},"count":60,"extra":true}`,
		`{"place":{"name":"a"},"other":{"name":"b"},"count":60,"label":null}`,
		`{"place":{"name":"a"},"other":{"name":"b"},"count":60.5}`,
		`{"place":{"name":"a"},"other":{"name":"b"},"count":101}`,
		`{"place":{"name":"a"},"other":{"name":"b"},"count":true}`,
		`{"place":{"name":"a"},"other":{"name":"b"},"count":null}`,
		`{broken`,
	} {
		if _, err := input.Decode(json.RawMessage(payload)); !errors.Is(err, ErrInvalidArguments) {
			t.Errorf("Decode(%s) error = %v, want ErrInvalidArguments", payload, err)
		}
	}
	// A schema default is only an annotation: the decoder does not fill absent data.
	optional, err := NewInput[struct {
		Count int `json:"count,omitempty" jsonschema:"default=3"`
	}](InputOptions{Name: "optional", Description: "Optional count"})
	if err != nil {
		t.Fatal(err)
	}
	decoded, err := optional.Decode(json.RawMessage(`{}`))
	if err != nil || decoded.Count != 0 {
		t.Fatalf("default filled missing data: %+v, %v", decoded, err)
	}
}

func TestInputBindingIsolation(t *testing.T) {
	options := InputOptions{Name: "bounded", Description: "Bounded input"}
	newBounded := func(maximum string) Input[struct {
		Count int `json:"count"`
	}] {
		t.Helper()
		options.Configure = func(schema *jsonschema.Schema) error {
			schema.Title = "Bounded input"
			property, found := schema.Properties.Get("count")
			if !found {
				t.Fatal("count property missing")
			}
			property.Maximum = json.Number(maximum)
			return nil
		}
		input, err := NewInput[struct {
			Count int `json:"count"`
		}](options)
		if err != nil {
			t.Fatal(err)
		}
		return input
	}
	low, high := newBounded("10"), newBounded("100")
	validForBoth := json.RawMessage(`{"count":10}`)
	validForHigh := json.RawMessage(`{"count":20}`)
	if _, err := low.Decode(validForBoth); err != nil {
		t.Fatal(err)
	}
	if _, err := high.Decode(validForHigh); err != nil {
		t.Fatal(err)
	}
	if _, err := low.Decode(validForHigh); !errors.Is(err, ErrInvalidArguments) {
		t.Fatalf("low binding accepted value above its cap: %v", err)
	}
}

func TestNewInputRejectsInvalidContracts(t *testing.T) {
	for _, options := range []InputOptions{
		{Name: "", Description: "Valid description"},
		{Name: "Invalid", Description: "Valid description"},
		{Name: "valid", Description: ""},
		{Name: "valid", Description: "Valid description", Configure: func(schema *jsonschema.Schema) error {
			schema.Ref = "https://example.invalid/foreign-schema"
			return nil
		}},
	} {
		if _, err := NewInput[testInput](options); err == nil {
			t.Errorf("NewInput(%+v) succeeded", options)
		}
	}
	if _, err := NewInput[string](InputOptions{Name: "valid", Description: "Valid description"}); err == nil {
		t.Fatal("non-object input succeeded")
	}
}
