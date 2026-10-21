package catalog

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"errors"
	"reflect"
	"slices"
	"testing"

	"github.com/santhosh-tekuri/jsonschema/v6"
)

// golden.json holds values of the `action_groups` setting, whether the Python service accepts each, and the catalog
// it holds for one it accepts; and input schemas with arguments, and what the Python service's admission makes of
// them. `generate_catalog_golden.py` writes it and `test_catalog_golden.py` checks it is current.
//
//go:embed golden.json
var goldenJSON []byte

const (
	health = "health is the MCP executor's report, and Python's own rendering of the setting leaves it out"
	// Pydantic's lax mode reads text and numbers as booleans and integers; yaml.v3 reads YAML's booleans, the YAML 1.1
	// words among them, and YAML's numbers.
	laxCoercion = "yaml.v3 does not coerce it as Pydantic's lax mode does"
	scalarText  = "yaml.v3 reads any scalar into a string"
	nullAbsent  = "yaml.v3 reads null as the field's absence"
)

// pythonAcceptsOnly and goAcceptsOnly are the values this catalog and the Python service disagree on, with why.
var (
	pythonAcceptsOnly = map[string]string{
		"configured health":                            health,
		"available as a digit":                         laxCoercion,
		"available as a number":                        laxCoercion,
		"available as a float":                         laxCoercion,
		"limits as integer text":                       laxCoercion,
		"a limit as integer text with a zero fraction": laxCoercion,
		"limits as booleans":                           laxCoercion,
	}
	goAcceptsOnly = map[string]string{
		"title as a number":                         scalarText,
		"action title as a number":                  scalarText,
		"annotation title as a number":              scalarText,
		"template description that is not a string": scalarText,
		"null":                      nullAbsent,
		"null available":            nullAbsent,
		"null actions":              nullAbsent,
		"null input schema":         nullAbsent,
		"null MCP config":           nullAbsent,
		"null max_timeout_seconds":  nullAbsent,
		"null template description": nullAbsent,
	}
)

func TestActionGroupsAreAcceptedAndHeldAsPythonDoes(t *testing.T) {
	var golden struct {
		ActionGroups []struct {
			Name  string          `json:"name"`
			Input json.RawMessage `json:"input"`
			Valid bool            `json:"valid"`
			Held  json.RawMessage `json:"held"`
		} `json:"action_groups"`
	}
	if err := json.Unmarshal(goldenJSON, &golden); err != nil {
		t.Fatal(err)
	}
	for _, vector := range golden.ActionGroups {
		t.Run(vector.Name, func(t *testing.T) {
			c, err := parse(asYAML(t, vector.Input))
			accepted := err == nil
			if reason, ok := pythonAcceptsOnly[vector.Name]; ok {
				if !vector.Valid || accepted {
					t.Errorf("Python accepts it: %v; Parse: %v; only Python accepts it because %s", vector.Valid, err, reason)
				}
				return
			}
			if reason, ok := goAcceptsOnly[vector.Name]; ok {
				if vector.Valid || !accepted {
					t.Errorf("Python accepts it: %v; Parse: %v; only this catalog accepts it because %s", vector.Valid, err, reason)
				}
				return
			}
			if accepted != vector.Valid {
				t.Fatalf("Python accepts it: %v; Parse: %v", vector.Valid, err)
			}
			if !accepted {
				return
			}
			held, err := json.Marshal(dump(c))
			if err != nil {
				t.Fatal(err)
			}
			var got, want any
			if err := json.Unmarshal(held, &got); err != nil {
				t.Fatal(err)
			}
			if err := json.Unmarshal(vector.Held, &want); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, want) {
				t.Errorf("holds  %s\nPython %s", held, vector.Held)
			}
		})
	}
}

// asYAML is a JSON document as YAML: the same JSON, with each character written out. Python's json.dumps escapes a
// character beyond the Basic Multilingual Plane as a surrogate pair, which yaml.v3 refuses.
func asYAML(t *testing.T, document json.RawMessage) string {
	t.Helper()
	decoder := json.NewDecoder(bytes.NewReader(document))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		t.Fatal(err)
	}
	written, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return string(written)
}

// dump is the catalog in the fields of the Python service's models.
func dump(c *Catalog) map[string]any {
	groups := map[string]any{}
	for key, g := range c.groups {
		var executor map[string]any
		switch binding := g.Executor.(type) {
		case MCPBinding:
			executor = map[string]any{"kind": "mcp", "description": binding.Description, "config": binding.Config}
		case PodBinding:
			executor = map[string]any{
				"kind": "pod", "description": binding.Description, "namespace": binding.Namespace,
				"templates": binding.Templates, "max_timeout_seconds": binding.MaxTimeoutSeconds,
				"max_output_bytes": binding.MaxOutputBytes,
			}
		}
		actions := map[string]any{}
		for name, action := range g.Actions {
			var annotations map[string]any
			if a := action.Annotations; a != nil {
				annotations = map[string]any{
					"title": a.Title, "read_only_hint": a.ReadOnlyHint, "destructive_hint": a.DestructiveHint,
					"idempotent_hint": a.IdempotentHint, "open_world_hint": a.OpenWorldHint,
				}
			}
			actions[name] = map[string]any{
				"description": action.Description, "input_schema": action.InputSchema, "title": action.Title,
				"annotations": annotations,
			}
		}
		var directTools []string
		for tool := range g.DirectTools {
			directTools = append(directTools, tool)
		}
		slices.Sort(directTools)
		groups[key] = map[string]any{
			"title": g.Title, "description": g.Description, "executor": executor, "available": g.Available,
			"actions": actions, "direct_tools": directTools,
		}
	}
	return groups
}

// argumentDifferences are the input schemas and arguments on which CheckArguments and Python's admission disagree,
// with why. Each must still differ, so the list cannot outlive what it excuses.
var argumentDifferences = map[string]string{
	"a regex format in draft-07, not a regex": "the library asserts `regex` before draft 2019-09 and takes no annotation for it",
	"dependencies in 2020-12":                 "the library honours draft 7's dependencies in every draft",
	"an unknown $schema":                      "Python validates as draft 2020-12; the unknown metaschema is not loaded, so the schema cannot be used",
	"0.3 as a multiple of 0.1":                "exact decimal arithmetic; Python divides in binary floating point",
	"a lookahead pattern":                     "RE2 has no lookaround, so the schema cannot be used",
}

func TestArgumentsAreCheckedAsPythonsAdmissionChecksThem(t *testing.T) {
	var golden struct {
		Arguments []struct {
			Name      string          `json:"name"`
			Schema    json.RawMessage `json:"schema"`
			Arguments json.RawMessage `json:"arguments"`
			Outcome   string          `json:"outcome"`
		} `json:"arguments"`
	}
	if err := json.Unmarshal(goldenJSON, &golden); err != nil {
		t.Fatal(err)
	}
	for _, vector := range golden.Arguments {
		t.Run(vector.Name, func(t *testing.T) {
			arguments, err := jsonschema.UnmarshalJSON(bytes.NewReader(vector.Arguments))
			if err != nil {
				t.Fatal(err)
			}
			var got string
			switch err := (ActionDefinition{InputSchema: vector.Schema}).CheckArguments(arguments); {
			case err == nil:
				got = "satisfied"
			case errors.Is(err, ErrInvalidArguments):
				got = "not_satisfied"
			default:
				got = "fault"
			}
			if reason, differs := argumentDifferences[vector.Name]; differs {
				if got == vector.Outcome {
					t.Errorf("%s, as Python's admission; listed as differing because %s", got, reason)
				}
				return
			}
			if got != vector.Outcome {
				t.Errorf("%s; Python's admission: %s", got, vector.Outcome)
			}
		})
	}
}
