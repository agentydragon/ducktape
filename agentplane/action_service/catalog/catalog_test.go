package catalog

import (
	"errors"
	"strings"
	"testing"

	"gopkg.in/yaml.v3"
)

// parse decodes document as the service decodes its settings file, and builds the catalog.
func parse(document string) (*Catalog, error) {
	var settings Settings
	decoder := yaml.NewDecoder(strings.NewReader(document))
	decoder.KnownFields(true)
	if err := decoder.Decode(&settings); err != nil {
		return nil, err
	}
	return New(settings)
}

func TestResolveFindsOnlyTheActionsOfAvailableGroups(t *testing.T) {
	c, err := parse(`{
		"test-group": {"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e"},
			"actions": {"test-action": {"description": "the offered Action"}}},
		"test-offline": {"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e"},
			"available": false, "actions": {"test-action": {"description": "d"}}}
	}`)
	if err != nil {
		t.Fatal(err)
	}
	group, action, err := c.Resolve("test-group", "test-action")
	if err != nil || group.Title != "t" || action.Description != "the offered Action" {
		t.Errorf("Resolve = %+v, %+v, %v", group, action, err)
	}
	for _, unresolved := range []struct {
		group, action string
		want          error
	}{
		{"test-unknown", "test-action", ErrUnknownAction},
		{"test-group", "test-unknown", ErrUnknownAction},
		{"test-offline", "test-action", ErrUnavailable},
		{"test-offline", "test-unknown", ErrUnavailable},
	} {
		if _, _, err := c.Resolve(unresolved.group, unresolved.action); !errors.Is(err, unresolved.want) {
			t.Errorf("Resolve(%s, %s) = %v, want %v", unresolved.group, unresolved.action, err, unresolved.want)
		}
	}
}

func TestSessionDelegationBindingIsStrictAndTyped(t *testing.T) {
	c, err := parse(`{"sessions": {"title": "Sessions", "description": "delegated Sessions",
		"executor": {"kind": "session_delegation", "description": "forward caller identity"},
		"actions": {"create": {"description": "create a Session"}}}}`)
	if err != nil {
		t.Fatal(err)
	}
	group, _, err := c.Resolve("sessions", "create")
	if err != nil {
		t.Fatal(err)
	}
	if binding, ok := group.Executor.(SessionDelegationBinding); !ok || binding.Description != "forward caller identity" {
		t.Fatalf("executor = %#v, want a typed session delegation binding", group.Executor)
	}
	if _, err := parse(`{"sessions": {"title": "Sessions", "description": "delegated Sessions",
		"executor": {"kind": "session_delegation", "description": "forward caller identity", "namespace": "unexpected"}}}`); err == nil {
		t.Fatal("accepted an unsupported session_delegation executor field")
	}
}

func TestJSONObjectChecksMapKeysInsideSequences(t *testing.T) {
	for _, test := range []struct {
		name    string
		value   map[string]any
		want    string
		wantErr bool
	}{
		{
			name:    "non-string key",
			value:   map[string]any{"items": []any{map[any]any{1: "sample"}}},
			wantErr: true,
		},
		{
			name:  "string key",
			value: map[string]any{"items": []any{map[string]any{"key": "value"}}},
			want:  `{"items":[{"key":"value"}]}`,
		},
	} {
		t.Run(test.name, func(t *testing.T) {
			got, err := jsonObject(test.value)
			if test.wantErr {
				if err == nil {
					t.Fatal("accepted a non-string map key inside a sequence")
				}
				return
			}
			if err != nil {
				t.Fatalf("jsonObject() error = %v", err)
			}
			if string(got) != test.want {
				t.Errorf("jsonObject() = %s, want %s", got, test.want)
			}
		})
	}
}

func TestNewRejectsCyclicProgrammaticSettings(t *testing.T) {
	mapCycle := map[string]any{}
	mapCycle["self"] = mapCycle

	sliceCycle := make([]any, 1)
	sliceCycle[0] = sliceCycle

	var pointerCycle any
	pointerCycle = &pointerCycle

	for name, value := range map[string]any{
		"map":     mapCycle,
		"slice":   sliceCycle,
		"pointer": pointerCycle,
	} {
		t.Run(name, func(t *testing.T) {
			settings := Settings{
				"group": {
					Title:       "Group",
					Description: "A group",
					Executor: &executorSettings{kind: &mcpExecutorSettings{
						Description: "An MCP server",
						Config:      map[string]any{"value": value},
					}},
				},
			}
			if _, err := New(settings); err == nil {
				t.Fatal("accepted cyclic programmatic settings")
			}
		})
	}
}

func TestJSONObjectAllowsSharedAcyclicValues(t *testing.T) {
	shared := map[string]any{"key": "value"}
	got, err := jsonObject(map[string]any{"first": shared, "second": shared})
	if err != nil {
		t.Fatalf("jsonObject() error = %v", err)
	}
	if want := `{"first":{"key":"value"},"second":{"key":"value"}}`; string(got) != want {
		t.Errorf("jsonObject() = %s, want %s", got, want)
	}
}

// New's own refusals of what yaml.v3 decodes; config_test holds yaml.v3's.
func TestRefusalsDoNotEchoValues(t *testing.T) {
	for name, document := range map[string]string{
		"server_id":                 `{"g": {"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e", "config": {"server_id": "never-print-this"}}}}`,
		"server_id of another type": `{"g": {"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e", "config": {"server_id": ["never-print-this"]}}}}`,
		"long title":                `{"g": {"title": "never-print-this` + strings.Repeat("x", 200) + `", "description": "d", "executor": {"kind": "mcp", "description": "e"}}}`,
		"direct tool":               `{"g": {"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e"}, "direct_tools": ["never-print-this.x"]}}`,
	} {
		t.Run(name, func(t *testing.T) {
			var settings Settings
			if err := yaml.Unmarshal([]byte(document), &settings); err != nil {
				t.Fatal(err)
			}
			_, err := New(settings)
			if err == nil {
				t.Fatal("accepted")
			}
			if strings.Contains(err.Error(), "never-print-this") {
				t.Errorf("the error carries a value: %v", err)
			}
		})
	}
}

func TestAnUpdateReplacesTheGroupAndWakesTheReadersWaitingForIt(t *testing.T) {
	c, err := parse(`{"test-group": {"title": "t", "description": "d", "executor": {"kind": "mcp", "description": "e"},
		"actions": {"test-action": {"description": "d"}}}}`)
	if err != nil {
		t.Fatal(err)
	}
	changed := c.Changed()
	held, _, err := c.Resolve("test-group", "test-action")
	if err != nil {
		t.Fatal(err)
	}
	c.Update("test-group", func(group *Group) { group.Available = false })
	select {
	case <-changed:
	default:
		t.Error("the Update woke no reader")
	}
	if !held.Available {
		t.Error("the group a reader held changed under it")
	}
	if _, _, err := c.Resolve("test-group", "test-action"); !errors.Is(err, ErrUnavailable) {
		t.Errorf("Resolve after the Update = %v, want %v", err, ErrUnavailable)
	}
	select {
	case <-c.Changed():
		t.Error("a reader waiting from now on is woken before the next Update")
	default:
	}
}
