package contract

import (
	"encoding/json"
	"maps"
	"reflect"
	"slices"
	"strings"
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
	if got, want := slices.Sorted(maps.Keys(environments)), []string{"get", "list", "resume"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("environment actions = %v, want %v", got, want)
	}
	if got, want := slices.Sorted(maps.Keys(sessions)), []string{"cancel_input", "create", "get", "interrupt_turn", "observe_input", "read_turns", "submit_input"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("session actions = %v, want %v", got, want)
	}
	assertProperties(t, inputs.ListEnvironments.Definition().InputSchema, []string{"after", "limit"}, []string{"limit"})
	assertProperties(t, inputs.GetEnvironment.Definition().InputSchema,
		[]string{"target"}, []string{"target"})
	assertProperties(t, inputs.ResumeEnvironment.Definition().InputSchema, []string{"resume_token", "target"}, []string{"resume_token", "target"})
	assertProperties(t, inputs.CreateSession.Definition().InputSchema,
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

func TestCreateSessionInstructionDescriptionKeepsSessionSeparateFromInput(t *testing.T) {
	inputs, err := NewInputs()
	if err != nil {
		t.Fatal(err)
	}
	var open map[string]any
	if err := json.Unmarshal(inputs.CreateSession.Definition().InputSchema, &open); err != nil {
		t.Fatal(err)
	}
	description := open["properties"].(map[string]any)["additional_instructions"].(map[string]any)["description"].(string)
	if description != "Standing instructions added to the Session. This field does not submit input or start a model turn. After creation, call sessions__submit_input with the task to start work." {
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
	resumeArguments, err := json.Marshal(map[string]any{
		"target":       map[string]string{"namespace": "agentplane-staging", "name": "sandbox-one", "uid": "uid-1"},
		"resume_token": strings.Repeat("x", 2049),
	})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := inputs.ResumeEnvironment.Decode(resumeArguments); err == nil {
		t.Fatal("ResumeEnvironment.Decode accepted an oversized token")
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
		if _, err := inputs.CreateSession.Decode(arguments); err != nil {
			t.Errorf("CreateSession.Decode rejected offered harness %s: %v", harness, err)
		}
	}
	const invalidHarness = `{"target":{"namespace":"agentplane-staging","name":"sandbox-one","uid":"uid-1"},"session_id":"11111111-1111-4111-8111-111111111111","spec":{"harness":"HARNESS_UNSPECIFIED","model":"model","cwd":"/workspace"}}`
	if _, err := inputs.CreateSession.Decode(json.RawMessage(invalidHarness)); err == nil {
		t.Fatal("CreateSession.Decode accepted the unspecified harness")
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
	if got := slices.Sorted(maps.Keys(object.Properties)); !reflect.DeepEqual(got, wantProperties) {
		t.Errorf("schema properties = %v, want %v", got, wantProperties)
	}
	gotRequired := append([]string(nil), object.Required...)
	wantRequiredSorted := append([]string(nil), wantRequired...)
	slices.Sort(gotRequired)
	slices.Sort(wantRequiredSorted)
	if !reflect.DeepEqual(gotRequired, wantRequiredSorted) {
		t.Errorf("schema required = %v, want %v", gotRequired, wantRequired)
	}
	if object.Additional == nil || *object.Additional {
		t.Errorf("schema additionalProperties = %v, want false", object.Additional)
	}
}
