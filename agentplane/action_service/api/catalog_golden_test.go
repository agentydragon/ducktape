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

// pythonExecutorKinds are the executor kinds as the Python service's ActionGroupView names them.
var pythonExecutorKinds = map[action_servicepb.ExecutorKind]string{
	action_servicepb.ExecutorKind_EXECUTOR_KIND_MCP: "mcp",
	action_servicepb.ExecutorKind_EXECUTOR_KIND_POD: "pod",
}

// The catalog vectors (`generate_catalog_golden.py`) hold, for each `action_groups` value the Python service accepts,
// the ActionGroupViews a workload reads of it there. Each catalog this service also accepts must read the same in the
// fields Python's views have, but in order: Python lists the groups and their Actions in the order of the settings
// file, which the decoded settings do not keep, and this service in key order. So both are compared by key.
func TestTheCatalogReadsAsThePythonServicesDoes(t *testing.T) {
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
			Name  string           `json:"name"`
			Input json.RawMessage  `json:"input"`
			Valid bool             `json:"valid"`
			Views []map[string]any `json:"views"`
		} `json:"action_groups"`
	}
	if err := json.Unmarshal(document, &golden); err != nil {
		t.Fatal(err)
	}
	var compared int
	for _, vector := range golden.ActionGroups {
		if !vector.Valid {
			continue
		}
		// The catalog's own vector test pins which values only Python accepts.
		groups, err := parseCatalog(asYAML(t, vector.Input))
		if err != nil {
			continue
		}
		compared++
		t.Run(vector.Name, func(t *testing.T) {
			views, err := groupViews(groups, false)
			if err != nil {
				t.Fatal(err)
			}
			got := map[string]any{}
			for _, view := range views {
				got[view.GetKey()] = pythonView(view)
			}
			want := map[string]any{}
			for _, view := range vector.Views {
				actions := map[string]any{}
				for _, action := range view["actions"].([]any) {
					actions[action.(map[string]any)["name"].(string)] = action
				}
				view["actions"] = actions
				// Health is the MCP executor's report, which no setting both services accept configures.
				view["health"] = view["health"] != nil
				want[view["key"].(string)] = view
			}
			if got, want := normalized(t, got), normalized(t, want); !reflect.DeepEqual(got, want) {
				t.Errorf("reads  %v\nPython %v", got, want)
			}
		})
	}
	if compared == 0 {
		t.Fatal("no catalog both services accept")
	}
}

// pythonView is a view in the fields of the Python service's ActionGroupView, with its Actions by name and whether it
// carries health.
func pythonView(view *action_servicepb.ActionGroup) map[string]any {
	actions := map[string]any{}
	for _, action := range view.GetActions() {
		actions[action.GetName()] = map[string]any{
			"group": action.GetGroup(), "name": action.GetName(), "description": action.GetDescription(),
			"input_schema": action.GetInputSchema().AsMap(),
		}
	}
	return map[string]any{
		"key": view.GetKey(), "title": view.GetTitle(), "description": view.GetDescription(),
		"executor_kind": pythonExecutorKinds[view.GetExecutorKind()], "executor_description": view.GetExecutorDescription(),
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
