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

// golden.json holds explicit catalog and argument-validation contract vectors for this Go service.
//
//go:embed golden.json
var contractVectors []byte

func TestActionGroupsMatchGoContract(t *testing.T) {
	var golden struct {
		ActionGroups []struct {
			Name     string          `json:"name"`
			Input    json.RawMessage `json:"input"`
			Accepted bool            `json:"accepted_by_go"`
			Held     json.RawMessage `json:"held"`
		} `json:"action_groups"`
	}
	if err := json.Unmarshal(contractVectors, &golden); err != nil {
		t.Fatal(err)
	}
	for _, vector := range golden.ActionGroups {
		t.Run(vector.Name, func(t *testing.T) {
			c, err := parse(asYAML(t, vector.Input))
			accepted := err == nil
			if accepted != vector.Accepted {
				t.Fatalf("accepted = %v, parse error = %v; want accepted = %v", accepted, err, vector.Accepted)
			}
			if !accepted {
				return
			}
			if len(vector.Held) == 0 {
				t.Fatal("accepted vector has no expected catalog")
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
				t.Errorf("holds  %s\nwant %s", held, vector.Held)
			}
		})
	}
}

// asYAML decodes and re-encodes a JSON document so yaml.v3 receives Unicode code points rather than surrogate pairs.
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

// dump is the catalog's settings with defaults and absent values made explicit.
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
		case OrchestrationBinding:
			executor = map[string]any{
				"kind": "orchestration_v1", "description": binding.Description, "namespace": binding.Namespace,
				"default_policy_sets": binding.DefaultPolicySets, "presets": binding.Presets,
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

func TestArgumentsMatchGoContract(t *testing.T) {
	var golden struct {
		Arguments []struct {
			Name      string          `json:"name"`
			Schema    json.RawMessage `json:"schema"`
			Arguments json.RawMessage `json:"arguments"`
			Outcome   string          `json:"go_outcome"`
		} `json:"arguments"`
	}
	if err := json.Unmarshal(contractVectors, &golden); err != nil {
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
			if got != vector.Outcome {
				t.Errorf("got %s; want %s", got, vector.Outcome)
			}
		})
	}
}
