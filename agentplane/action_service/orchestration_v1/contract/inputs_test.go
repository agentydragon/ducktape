package contract

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/action_service/catalog"
)

func TestFourCommandInputsAdvertiseAndDecodeTheirOwnRequiredFields(t *testing.T) {
	inputs, err := NewInputs()
	if err != nil {
		t.Fatal(err)
	}
	base := `"sandbox":{"name":"test-worker","uid":"00000000-0000-0000-0000-000000000000"},` +
		`"session_id":"00000000-0000-0000-0000-000000000000","command_id":"00000000-0000-0000-0000-000000000000",`
	for _, test := range []struct {
		name  string
		input commandInput
		field string
		value string
	}{
		{"submit_input", inputFor(inputs.SubmitInput), "text", `"hello"`},
		{"interrupt_turn", inputFor(inputs.InterruptTurn), "turn_id", `"test-turn"`},
		{"change_model", inputFor(inputs.ChangeModel), "model", `"test-model"`},
		{"change_reasoning_effort", inputFor(inputs.ChangeReasoningEffort), "effort", `"high"`},
	} {
		t.Run(test.name, func(t *testing.T) {
			valid := `{"` + test.field + `":` + test.value + `,` + base[:len(base)-1] + `}`
			checkInputBoundary(t, test.input, valid, true)
			missing := `{` + base[:len(base)-1] + `}`
			checkInputBoundary(t, test.input, missing, false)
		})
	}
}

func TestNumericInputsKeepIntegralJSONNumberSpellingsAndBounds(t *testing.T) {
	inputs, err := NewInputs()
	if err != nil {
		t.Fatal(err)
	}
	read := inputFor(inputs.ReadTurns)
	for _, number := range []string{"60.0", "6e1"} {
		raw := `{"sandbox":{"name":"test-worker","uid":"00000000-0000-0000-0000-000000000000"},"session_id":"00000000-0000-0000-0000-000000000000","count":` + number + `}`
		checkInputBoundary(t, read, raw, true)
	}
	for _, number := range []string{"60.5", "null", "0", "101", "9223372036854775808"} {
		raw := `{"sandbox":{"name":"test-worker","uid":"00000000-0000-0000-0000-000000000000"},"session_id":"00000000-0000-0000-0000-000000000000","count":` + number + `}`
		checkInputBoundary(t, read, raw, false)
	}

	launch := inputFor(inputs.LaunchSandbox)
	for _, number := range []string{"60.0", "6e1"} {
		raw := `{"name":"test-worker","template":"test-template","idle_suspend_after_seconds":` + number + `}`
		checkInputBoundary(t, launch, raw, true)
	}
	for _, number := range []string{"59", "60.5", "31536001", "null"} {
		raw := `{"name":"test-worker","template":"test-template","idle_suspend_after_seconds":` + number + `}`
		checkInputBoundary(t, launch, raw, false)
	}

	wait := inputFor(inputs.WaitForTurn)
	waitBase := `{"sandbox":{"name":"test-worker","uid":"00000000-0000-0000-0000-000000000000"},"session_id":"00000000-0000-0000-0000-000000000000","resume_token":"test-token","wait_seconds":`
	checkInputBoundary(t, wait, waitBase+`0.5}`, true)
	checkInputBoundary(t, wait, waitBase+`30.0}`, true)
	checkInputBoundary(t, wait, waitBase+`30.01}`, false)
	checkInputBoundary(t, wait, waitBase+`null}`, false)
}

func TestOptionalPointersRejectNullAndPreserveAbsentDefaults(t *testing.T) {
	inputs, err := NewInputs()
	if err != nil {
		t.Fatal(err)
	}
	base := `{"sandbox":{"name":"test-worker","uid":"00000000-0000-0000-0000-000000000000"},"session_id":"00000000-0000-0000-0000-000000000000"`
	spec := `"spec":{"harness":"HARNESS_CODEX","model":"test-model","cwd":"/test"}`
	input := inputFor(inputs.OpenSession)
	input.decode = func(raw json.RawMessage) error {
		args, err := inputs.OpenSession.Decode(raw)
		if err == nil && (args.SetupScript != nil || args.Spec.ReasoningEffort != "" || args.Spec.Instructions != "") {
			t.Errorf("absent optional fields decoded as %+v", args)
		}
		return err
	}
	checkInputBoundary(t, input, base+`,`+spec+`}`, true)
	checkInputBoundary(t, input, base+`,`+spec+`,"setup_script":null}`, false)
	checkInputBoundary(t, input, base+`,"spec":{"harness":"HARNESS_CODEX","model":"test-model","cwd":"/test","instructions":null}}`, false)
	checkInputBoundary(t, input, base+`,"spec":{"harness":"HARNESS_UNSPECIFIED","model":"test-model","cwd":"/test"}}`, false)
	checkInputBoundary(t, input, `{"sandbox":{"name":"test-worker","uid":"00000000-0000-0000-0000-000000000000","extra":true},"session_id":"00000000-0000-0000-0000-000000000000",`+spec+`}`, false)
	checkInputBoundary(t, input, base+`,"spec":{"harness":"HARNESS_CODEX","model":"test-model","cwd":"/test","extra":true}}`, false)
}

func TestLaunchArraysKeepUniquenessItemBoundsAndGrantEnum(t *testing.T) {
	inputs, err := NewInputs()
	if err != nil {
		t.Fatal(err)
	}
	input := inputFor(inputs.LaunchSandbox)
	base := `{"name":"test-worker","template":"test-template"`
	checkInputBoundary(t, input, base+`}`, true)
	for _, invalid := range []string{
		base + `,"policy_sets":["policy","policy"]}`,
		base + `,"policy_sets":[""]}`,
		base + `,"inbox_tags":["tag","tag"]}`,
		base + `,"inbox_tags":[""]}`,
		base + `,"inbox_tags":["` + strings.Repeat("x", 64) + `"]}`,
		base + `,"launcher_grants":["send_message","send_message"]}`,
		base + `,"launcher_grants":["unknown_action"]}`,
	} {
		checkInputBoundary(t, input, invalid, false)
	}
}

type commandInput struct {
	name       string
	definition catalog.ActionDefinition
	decode     func(json.RawMessage) error
}

func checkInputBoundary(t *testing.T, input commandInput, raw string, accepted bool) {
	t.Helper()
	var value any
	if err := json.Unmarshal([]byte(raw), &value); err != nil {
		t.Fatal(err)
	}
	schemaErr := input.definition.CheckArguments(value)
	decodeErr := input.decode(json.RawMessage(raw))
	if (schemaErr == nil) != accepted || (decodeErr == nil) != accepted {
		t.Errorf("%s boundary for %s: schema error %v, decode error %v, accepted=%t", input.name, raw, schemaErr, decodeErr, accepted)
	}
}

func inputFor[T any](input catalog.Input[T]) commandInput {
	return commandInput{
		name: input.Name(), definition: input.Definition(), decode: func(raw json.RawMessage) error {
			_, err := input.Decode(raw)
			return err
		},
	}
}
