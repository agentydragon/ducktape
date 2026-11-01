package contract

import (
	"encoding/json"
	"reflect"
	"testing"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
)

func TestInputsUseApprovedActionNamesAndSchemas(t *testing.T) {
	inputs, err := NewInputs()
	if err != nil {
		t.Fatal(err)
	}
	environments := inputs.EnvironmentActions()
	sessions := inputs.SessionActions()
	if got, want := sortedKeys(environments), []string{"get", "list"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("environment actions = %v, want %v", got, want)
	}
	if got, want := sortedKeys(sessions), []string{"cancel_input", "get", "interrupt_turn", "observe_input", "open", "read_turns", "submit_input"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("session actions = %v, want %v", got, want)
	}
	assertProperties(t, inputs.ListEnvironments.Definition().InputSchema, []string{"after", "limit"}, []string{"limit"})
	assertProperties(t, inputs.GetEnvironment.Definition().InputSchema, []string{"target"}, []string{"target"})
	assertProperties(t, inputs.OpenSession.Definition().InputSchema,
		[]string{"additional_instructions", "session_id", "setup_script", "spec", "target"},
		[]string{"session_id", "spec", "target"})
	assertProperties(t, inputs.SubmitInput.Definition().InputSchema,
		[]string{"command_id", "session_id", "target", "text"}, []string{"command_id", "session_id", "target", "text"})
	assertProperties(t, inputs.ObserveInput.Definition().InputSchema,
		[]string{"command_id", "resume_token", "session_id", "wait_seconds"}, []string{"command_id", "session_id", "wait_seconds"})
	assertProperties(t, inputs.GetSession.Definition().InputSchema, []string{"session_id"}, []string{"session_id"})
	assertProperties(t, inputs.ReadTurns.Definition().InputSchema,
		[]string{"count", "page_cursor", "session_id"}, []string{"count", "session_id"})
	assertProperties(t, inputs.CancelInput.Definition().InputSchema,
		[]string{"command_id", "session_id"}, []string{"command_id", "session_id"})
	assertProperties(t, inputs.InterruptTurn.Definition().InputSchema,
		[]string{"command_id", "session_id", "target", "turn_id"}, []string{"command_id", "session_id", "target", "turn_id"})

}

func TestMutationActionsDoNotAdvertiseUnimplementedComposites(t *testing.T) {
	inputs, err := NewInputs()
	if err != nil {
		t.Fatal(err)
	}
	if got, want := sortedKeys(inputs.SessionMutationActions()), []string{"cancel_input", "interrupt_turn", "observe_input", "open", "submit_input"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("implemented session actions = %v, want %v", got, want)
	}
	var open map[string]any
	if err := json.Unmarshal(inputs.OpenSession.Definition().InputSchema, &open); err != nil {
		t.Fatal(err)
	}
	description := open["properties"].(map[string]any)["additional_instructions"].(map[string]any)["description"].(string)
	if description != "Standing instructions added to the Session. This field does not submit input or start a model turn. After opening, call sessions__submit_input with the task to start work." {
		t.Fatalf("additional_instructions description = %q", description)
	}
}

func TestInputDecoderRejectsUnknownFieldsAndInvalidEnums(t *testing.T) {
	inputs, err := NewInputs()
	if err != nil {
		t.Fatal(err)
	}
	for _, raw := range []string{
		`{"session_id":"11111111-1111-4111-8111-111111111111","command_id":"22222222-2222-4222-8222-222222222222","surprise":true}`,
		`{"session_id":"not-a-uuid","command_id":"22222222-2222-4222-8222-222222222222"}`,
	} {
		if _, err := inputs.CancelInput.Decode(json.RawMessage(raw)); err == nil {
			t.Errorf("CancelInput.Decode(%s) unexpectedly succeeded", raw)
		}
	}
	for _, harness := range []sessionpb.Harness{
		sessionpb.Harness_HARNESS_CLAUDE,
		sessionpb.Harness_HARNESS_CODEX,
		sessionpb.Harness_HARNESS_CENTRAL_SAMPLER,
	} {
		arguments, err := json.Marshal(map[string]any{
			"target":     map[string]string{"namespace": "agentplane-staging", "name": "sandbox-one", "uid": "uid-1"},
			"session_id": "11111111-1111-4111-8111-111111111111",
			"spec":       map[string]any{"harness": harness.String(), "model": "model", "cwd": "/workspace"},
		})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := inputs.OpenSession.Decode(arguments); err != nil {
			t.Errorf("OpenSession.Decode rejected offered harness %s: %v", harness, err)
		}
	}
	const invalidHarness = `{"target":{"namespace":"agentplane-staging","name":"sandbox-one","uid":"uid-1"},"session_id":"11111111-1111-4111-8111-111111111111","spec":{"harness":"HARNESS_UNSPECIFIED","model":"model","cwd":"/workspace"}}`
	if _, err := inputs.OpenSession.Decode(json.RawMessage(invalidHarness)); err == nil {
		t.Fatal("OpenSession.Decode accepted the unspecified harness")
	}
}

func assertProperties(t *testing.T, schema json.RawMessage, wantProperties, wantRequired []string) {
	t.Helper()
	var object struct {
		Properties map[string]json.RawMessage `json:"properties"`
		Required   []string                   `json:"required"`
		Additional *bool                      `json:"additionalProperties"`
	}
	if err := json.Unmarshal(schema, &object); err != nil {
		t.Fatalf("unmarshal schema: %v", err)
	}
	if got := sortedKeys(object.Properties); !reflect.DeepEqual(got, wantProperties) {
		t.Errorf("schema properties = %v, want %v", got, wantProperties)
	}
	if got := sortStrings(append([]string(nil), object.Required...)); !reflect.DeepEqual(got, sortStrings(append([]string(nil), wantRequired...))) {
		t.Errorf("schema required = %v, want %v", got, wantRequired)
	}
	if object.Additional == nil || *object.Additional {
		t.Errorf("schema additionalProperties = %v, want false", object.Additional)
	}
}

func sortedKeys[V any](values map[string]V) []string {
	keys := make([]string, 0, len(values))
	for key := range values {
		keys = append(keys, key)
	}
	// The expected keys are small fixed lists, so sorting here keeps the assertion dependency-free.
	return sortStrings(keys)
}

func sortStrings(values []string) []string {
	for i := range values {
		for j := i + 1; j < len(values); j++ {
			if values[j] < values[i] {
				values[i], values[j] = values[j], values[i]
			}
		}
	}
	return values
}
