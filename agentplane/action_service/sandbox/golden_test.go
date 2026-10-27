package sandbox

import (
	_ "embed"
	"encoding/json"
	"maps"
	"reflect"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action"
	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
)

// golden holds the Sandbox Action roster, argument validation, and object identity contracts.
//
//go:embed golden.json
var golden []byte

type vectors struct {
	Roster map[string]struct {
		Binding struct {
			Description       string            `json:"description"`
			Namespace         string            `json:"namespace"`
			Templates         map[string]string `json:"templates"`
			MaxTimeoutSeconds int               `json:"max_timeout_seconds"`
			MaxOutputBytes    int               `json:"max_output_bytes"`
		} `json:"binding"`
		Actions any `json:"actions"`
	} `json:"roster"`
	Arguments []struct {
		Action string          `json:"action"`
		Name   string          `json:"name"`
		Input  json.RawMessage `json:"input"`
		Valid  bool            `json:"valid"`
	} `json:"arguments"`
	Boxes []struct {
		Name       string                `json:"name"`
		Caller     action.ServiceAccount `json:"caller"`
		Box        string                `json:"box"`
		ObjectName string                `json:"object_name"`
		Labels     map[string]string     `json:"labels"`
	} `json:"boxes"`
}

func readVectors(t *testing.T) vectors {
	t.Helper()
	var read vectors
	if err := json.Unmarshal(golden, &read); err != nil {
		t.Fatal(err)
	}
	return read
}

// annotations is the catalog.ToolAnnotations fixture representation.
type annotations struct {
	Title           *string `json:"title"`
	ReadOnlyHint    *bool   `json:"read_only_hint"`
	DestructiveHint *bool   `json:"destructive_hint"`
	IdempotentHint  *bool   `json:"idempotent_hint"`
	OpenWorldHint   *bool   `json:"open_world_hint"`
}

// TestActionRoster holds each Action's title, annotations and argument schema, caps included, to the fixture.
// Descriptions are prose, and are not compared.
func TestActionRoster(t *testing.T) {
	for name, roster := range readVectors(t).Roster {
		t.Run(name, func(t *testing.T) {
			type action struct {
				Title       *string         `json:"title"`
				InputSchema json.RawMessage `json:"input_schema"`
				Annotations *annotations    `json:"annotations"`
			}
			offered := map[string]action{}
			for key, definition := range Actions(catalog.PodBinding(roster.Binding)) {
				offered[key] = action{definition.Title, definition.InputSchema, (*annotations)(definition.Annotations)}
			}
			document, err := json.Marshal(offered)
			if err != nil {
				t.Fatal(err)
			}
			var got any
			if err := json.Unmarshal(document, &got); err != nil {
				t.Fatal(err)
			}
			if !reflect.DeepEqual(got, roster.Actions) {
				t.Errorf("offers\n%s\nwant\n%v", document, roster.Actions)
			}
		})
	}
}

// arguments are what dispatch decodes each Action's arguments into.
var arguments = map[string]func() any{
	actionCreate:      func() any { return &createArgs{} },
	actionGetTemplate: func() any { return &templateArgs{} },
	actionExec:        func() any { return &execArgs{} },
	actionList:        func() any { return &struct{}{} },
	actionGet:         func() any { return &nameArgs{} },
	actionDispose:     func() any { return &nameArgs{} },
}

func TestArgumentValidation(t *testing.T) {
	executor, err := NewExecutor(catalog.PodBinding{}, nil)
	if err != nil {
		t.Fatal(err)
	}
	for _, vector := range readVectors(t).Arguments {
		t.Run(vector.Action+"/"+vector.Name, func(t *testing.T) {
			if err := executor.decode(vector.Action, vector.Input, arguments[vector.Action]()); (err == nil) != vector.Valid {
				t.Errorf("decode(%s) = %v; valid = %t", vector.Input, err, vector.Valid)
			}
		})
	}
}

// TestBoxIdentity pins the object name and labels used to find a caller's existing box.
func TestBoxIdentity(t *testing.T) {
	for _, vector := range readVectors(t).Boxes {
		t.Run(vector.Name, func(t *testing.T) {
			if got := objectName(vector.Caller, vector.Box); got != vector.ObjectName {
				t.Errorf("objectName = %q, want %q", got, vector.ObjectName)
			}
			if got := boxLabels(vector.Caller, vector.Box, "test-template"); !maps.Equal(got, vector.Labels) {
				t.Errorf("boxLabels = %v, want %v", got, vector.Labels)
			}
		})
	}
}
