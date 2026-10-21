package mcpexec

import (
	_ "embed"
	"encoding/json"
	"reflect"
	"slices"
	"strings"
	"testing"

	"github.com/modelcontextprotocol/go-sdk/mcp"
)

// golden.json holds MCP bindings' configs and whether the Python service accepts each, tool lists and the Actions it
// mirrors from each, and tool results and what an Execution keeps of each. `generate_mcp_executor_golden.py` writes
// it and `test_mcp_executor_golden.py` checks it is current.
//
//go:embed golden.json
var goldenJSON []byte

type golden struct {
	Configs []struct {
		Name   string          `json:"name"`
		Config json.RawMessage `json:"config"`
		Valid  bool            `json:"valid"`
	} `json:"configs"`
	Tools []struct {
		Name    string            `json:"name"`
		Tools   []json.RawMessage `json:"tools"`
		Invalid bool              `json:"invalid"`
		Actions map[string]any    `json:"actions"`
	} `json:"tools"`
	Results []struct {
		Name    string          `json:"name"`
		Result  json.RawMessage `json:"result"`
		Stored  map[string]any  `json:"stored"`
		Unknown bool            `json:"unknown"`
	} `json:"results"`
}

func readGolden(t *testing.T) golden {
	t.Helper()
	var vectors golden
	if err := json.Unmarshal(goldenJSON, &vectors); err != nil {
		t.Fatal(err)
	}
	return vectors
}

// pythonAcceptsOnly are the configs only the Python service accepts, with why.
var pythonAcceptsOnly = map[string]string{
	"http with an empty authority": "Pydantic reads `https:///mcp` as the host `mcp`; net/url reads an empty host",
}

func TestBindingsAreAcceptedAsPythonAcceptsThem(t *testing.T) {
	for _, vector := range readGolden(t).Configs {
		t.Run(vector.Name, func(t *testing.T) {
			_, err := parseServer(vector.Config)
			if reason, ok := pythonAcceptsOnly[vector.Name]; ok {
				if !vector.Valid || err == nil {
					t.Errorf("Python accepts it: %v; parseServer: %v; only Python accepts it because %s", vector.Valid, err, reason)
				}
				return
			}
			if (err == nil) != vector.Valid {
				t.Fatalf("Python accepts it: %v; parseServer: %v", vector.Valid, err)
			}
			// A config can hold a credential, so a refusal names fields, never a value but the words of the
			// transport and auth vocabularies.
			for _, value := range stringValues(t, vector.Config) {
				if err != nil && value != "" && !slices.Contains(vocabulary, value) && strings.Contains(err.Error(), value) {
					t.Errorf("the refusal %q quotes the value %q", err, value)
				}
			}
		})
	}
}

var vocabulary = []string{"stdio", "streamable-http", "none", "oauth", "static_bearer"}

// stringValues are the strings a JSON document holds as values.
func stringValues(t *testing.T, document json.RawMessage) []string {
	t.Helper()
	var value any
	if err := json.Unmarshal(document, &value); err != nil {
		t.Fatal(err)
	}
	var values []string
	var walk func(any)
	walk = func(v any) {
		switch v := v.(type) {
		case string:
			values = append(values, v)
		case []any:
			for _, item := range v {
				walk(item)
			}
		case map[string]any:
			for _, item := range v {
				walk(item)
			}
		}
	}
	walk(value)
	return values
}

// falseHintsAbsent is a mirrored tool list as this executor holds it: go-sdk reads an absent read-only or idempotent
// hint as false, which the protocol defines it to mean, so a false one is held as absent.
func falseHintsAbsent(actions map[string]any) {
	for _, action := range actions {
		if annotations, given := action.(map[string]any)["annotations"].(map[string]any); given {
			for _, hint := range []string{"read_only_hint", "idempotent_hint"} {
				if annotations[hint] == false {
					annotations[hint] = nil
				}
			}
		}
	}
}

func TestToolsAreMirroredAsPythonMirrorsThem(t *testing.T) {
	for _, vector := range readGolden(t).Tools {
		t.Run(vector.Name, func(t *testing.T) {
			listed := make([]*mcp.Tool, len(vector.Tools))
			for i, wire := range vector.Tools {
				if err := json.Unmarshal(wire, &listed[i]); err != nil {
					t.Fatal(err)
				}
			}
			tools, err := mirror(listed)
			if vector.Invalid {
				if _, refused := err.(*invalidCatalogError); !refused {
					t.Fatalf("mirror() = %v, %v; Python refuses the list", tools, err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			falseHintsAbsent(vector.Actions)
			got := map[string]any{}
			for name, tool := range tools {
				got[name] = pythonAction(t, tool)
			}
			if !reflect.DeepEqual(got, vector.Actions) {
				t.Errorf("mirrors %v\nPython  %v", got, vector.Actions)
			}
		})
	}
}

// pythonAction is a tool's Action in the fields of the Python service's ActionDefinition, as JSON reads them.
func pythonAction(t *testing.T, tool tool) any {
	t.Helper()
	d := tool.definition
	var annotations map[string]any
	if a := d.Annotations; a != nil {
		annotations = map[string]any{
			"title": a.Title, "read_only_hint": a.ReadOnlyHint, "destructive_hint": a.DestructiveHint,
			"idempotent_hint": a.IdempotentHint, "open_world_hint": a.OpenWorldHint,
		}
	}
	return roundTrip(t, map[string]any{
		"description": d.Description, "input_schema": d.InputSchema, "title": d.Title, "annotations": annotations,
	})
}

func roundTrip(t *testing.T, value any) any {
	t.Helper()
	encoded, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	var decoded any
	if err := json.Unmarshal(encoded, &decoded); err != nil {
		t.Fatal(err)
	}
	return decoded
}

func TestResultsAreKeptAsPythonKeepsThem(t *testing.T) {
	for _, vector := range readGolden(t).Results {
		t.Run(vector.Name, func(t *testing.T) {
			var result mcp.CallToolResult
			if err := json.Unmarshal(vector.Result, &result); err != nil {
				t.Fatal(err)
			}
			stored, err := storedResult(&result)
			if err != nil {
				t.Fatal(err)
			}
			// go-sdk leaves out an `isError` that is false, which the protocol defines its absence to mean.
			want := vector.Stored
			if want["isError"] == false {
				delete(want, "isError")
			}
			var got map[string]any
			if err := json.Unmarshal(stored, &got); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, want) {
				t.Errorf("keeps %s\nPython %v", stored, want)
			}
			if unknown := reportsUnknownOutcome(&result); unknown != vector.Unknown {
				t.Errorf("reports an unknown outcome: %t; Python: %t", unknown, vector.Unknown)
			}
		})
	}
}
