package api

import (
	"bytes"
	"encoding/json"
	"os"
	"reflect"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/bazelbuild/rules_go/go/runfiles"
)

// executorKindNames are the stable values used in the catalog view contract.
var executorKindNames = map[action_servicepb.ExecutorKind]string{
	action_servicepb.ExecutorKind_EXECUTOR_KIND_MCP:              "mcp",
	action_servicepb.ExecutorKind_EXECUTOR_KIND_POD:              "pod",
	action_servicepb.ExecutorKind_EXECUTOR_KIND_ORCHESTRATION_V1: "orchestration_v1",
}

// The explicit view expectations cover settings accepted by both catalog construction and the API projection.
func TestCatalogViewsMatchTheirContractFixtures(t *testing.T) {
	path, err := runfiles.Rlocation("_main/agentplane/action_service/catalog/golden.json")
	if err != nil {
		t.Fatal(err)
	}
	document, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var golden struct {
		ActionGroups []struct {
			Name     string           `json:"name"`
			Input    json.RawMessage  `json:"input"`
			Accepted bool             `json:"accepted_by_go"`
			Views    []map[string]any `json:"views"`
		} `json:"action_groups"`
	}
	if err := json.Unmarshal(document, &golden); err != nil {
		t.Fatal(err)
	}
	var compared int
	for _, vector := range golden.ActionGroups {
		if vector.Views == nil {
			continue
		}
		if !vector.Accepted {
			t.Fatalf("%q has view expectations but is not accepted by Go", vector.Name)
		}
		groups, err := parseCatalog(asYAML(t, vector.Input))
		if err != nil {
			t.Fatalf("%q is marked accepted by Go but does not parse: %v", vector.Name, err)
		}
		compared++
		t.Run(vector.Name, func(t *testing.T) {
			views, err := groupViews(groups, false)
			if err != nil {
				t.Fatal(err)
			}
			got := map[string]any{}
			for _, view := range views {
				got[view.GetKey()] = viewContract(view)
			}
			want := map[string]any{}
			for _, view := range vector.Views {
				actions := map[string]any{}
				for _, action := range view["actions"].([]any) {
					actions[action.(map[string]any)["name"].(string)] = action
				}
				view["actions"] = actions
				// Health is reported by an MCP executor at runtime, not configured in this fixture.
				view["health"] = view["health"] != nil
				want[view["key"].(string)] = view
			}
			if got, want := normalized(t, got), normalized(t, want); !reflect.DeepEqual(got, want) {
				t.Errorf("reads  %v\nwant %v", got, want)
			}
		})
	}
	if compared == 0 {
		t.Fatal("no catalog view has an explicit contract fixture")
	}
}

// viewContract represents an ActionGroup in the stable catalog view fields, with Actions by name.
func viewContract(view *action_servicepb.ActionGroup) map[string]any {
	actions := map[string]any{}
	for _, action := range view.GetActions() {
		actions[action.GetName()] = map[string]any{
			"group": action.GetGroup(), "name": action.GetName(), "description": action.GetDescription(),
			"input_schema": action.GetInputSchema().AsMap(),
		}
	}
	return map[string]any{
		"key": view.GetKey(), "title": view.GetTitle(), "description": view.GetDescription(),
		"executor_kind": executorKindNames[view.GetExecutorKind()], "executor_description": view.GetExecutorDescription(),
		"available": view.GetAvailable(), "health": view.GetHealth() != nil, "actions": actions,
	}
}

// normalized is value as JSON decodes it, so that both sides hold the same Go types.
func normalized(t *testing.T, value any) any {
	t.Helper()
	document, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	var decoded any
	if err := json.Unmarshal(document, &decoded); err != nil {
		t.Fatal(err)
	}
	return decoded
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
