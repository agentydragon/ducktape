package sandbox

import (
	_ "embed"
	"encoding/json"
	"maps"
	"reflect"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/sandbox/contract"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"

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
		Name       string               `json:"name"`
		Caller     store.ServiceAccount `json:"caller"`
		Box        string               `json:"box"`
		ObjectName string               `json:"object_name"`
		Labels     map[string]string    `json:"labels"`
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

// TestActionRoster holds each Action's title and annotations to the independent fixture. Argument acceptance is
// checked against the existing input vectors in TestArgumentValidation rather than comparing generator shape.
// Descriptions are prose, and are not compared.
func TestActionRoster(t *testing.T) {
	for name, roster := range readVectors(t).Roster {
		t.Run(name, func(t *testing.T) {
			type action struct {
				Title       *string      `json:"title"`
				Annotations *annotations `json:"annotations"`
			}
			executor, err := NewExecutor(catalog.PodBinding(roster.Binding), nil)
			if err != nil {
				t.Fatal(err)
			}
			offered := map[string]action{}
			definitions := executor.Actions()
			for key, definition := range definitions {
				offered[key] = action{definition.Title, (*annotations)(definition.Annotations)}
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

func TestArgumentValidation(t *testing.T) {
	executor, err := NewExecutor(catalog.PodBinding{
		MaxTimeoutSeconds: 3600,
		MaxOutputBytes:    1_000_000,
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
	offered := executor.Actions()
	for _, vector := range readVectors(t).Arguments {
		t.Run(vector.Action+"/"+vector.Name, func(t *testing.T) {
			if err := decodeContractInput(executor, vector.Action, vector.Input); (err == nil) != vector.Valid {
				t.Errorf("decode(%s) = %v; valid = %t", vector.Input, err, vector.Valid)
			}
			var document any
			if err := json.Unmarshal(vector.Input, &document); err != nil {
				t.Fatal(err)
			}
			if err := offered[vector.Action].CheckArguments(document); (err == nil) != vector.Valid {
				t.Errorf("advertised schema check = %v; valid = %t", err, vector.Valid)
			}
		})
	}
}

func decodeContractInput(executor *Executor, action string, arguments json.RawMessage) error {
	switch action {
	case string(contract.Create):
		_, err := executor.inputs.Create.Decode(arguments)
		return err
	case string(contract.GetTemplate):
		_, err := executor.inputs.GetTemplate.Decode(arguments)
		return err
	case string(contract.Exec):
		_, err := executor.inputs.DecodeExec.Decode(arguments)
		return err
	case string(contract.List):
		_, err := executor.inputs.List.Decode(arguments)
		return err
	case string(contract.Get):
		_, err := executor.inputs.Get.Decode(arguments)
		return err
	case string(contract.Dispose):
		_, err := executor.inputs.Dispose.Decode(arguments)
		return err
	default:
		return catalog.ErrInvalidArguments
	}
}

func TestBoundExecSchemaKeepsAdmissionCapsSeparateFromStoredDecode(t *testing.T) {
	lower, err := NewExecutor(catalog.PodBinding{MaxTimeoutSeconds: 1, MaxOutputBytes: 4}, nil)
	if err != nil {
		t.Fatal(err)
	}
	higher, err := NewExecutor(catalog.PodBinding{MaxTimeoutSeconds: 20, MaxOutputBytes: 40}, nil)
	if err != nil {
		t.Fatal(err)
	}
	check := func(t *testing.T, executor *Executor, input string, valid bool) {
		t.Helper()
		err := executor.Actions()[string(contract.Exec)].CheckArguments(mustJSON(t, json.RawMessage(input)))
		if (err == nil) != valid {
			t.Errorf("advertised schema check = %v; valid = %t", err, valid)
		}
	}
	check(t, lower, `{"name":"box","script":"true","timeout_seconds":1,"max_output_bytes":4}`, true)
	check(t, lower, `{"name":"box","script":"true","timeout_seconds":2,"max_output_bytes":4}`, false)
	check(t, higher, `{"name":"box","script":"true","timeout_seconds":20,"max_output_bytes":40}`, true)
	check(t, higher, `{"name":"box","script":"true","timeout_seconds":21,"max_output_bytes":40}`, false)
	check(t, lower, `{"name":"box","script":"true","timeout_seconds":20,"max_output_bytes":40}`, false)

	storedRequest := json.RawMessage(`{"name":"box","script":"true","timeout_seconds":3600,"max_output_bytes":1000000}`)
	if _, err := lower.inputs.DecodeExec.Decode(storedRequest); err != nil {
		t.Fatalf("stable execution input rejected an admitted request after a cap change: %v", err)
	}
	if _, err := lower.inputs.DecodeExec.Decode(json.RawMessage(`{"name":"box","script":"true","timeout_seconds":3601,"max_output_bytes":1000000}`)); err == nil {
		t.Fatal("stable execution input accepted a request above the group ceiling")
	}
}

func mustJSON(t *testing.T, input json.RawMessage) any {
	t.Helper()
	var value any
	if err := json.Unmarshal(input, &value); err != nil {
		t.Fatal(err)
	}
	return value
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
