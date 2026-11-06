package codec

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"google.golang.org/protobuf/proto"
)

const wrappedPolicy = `{"apiVersion":"agentplane.allegedly.works/v1alpha1","kind":"PolicySet","metadata":{"name":"fixture","namespace":"policies","labels":{"owner":"test"}},"spec":{"rules":[{"type":"action_auto_approve","policy":{"type":"exact_actions","actions":{"github":{"names":["get_file_contents","list_commits"]}}}}]}}`

func TestWrappedPolicyJSONAndDeepCopy(t *testing.T) {
	var object PolicySet
	if err := json.Unmarshal([]byte(wrappedPolicy), &object); err != nil {
		t.Fatal(err)
	}
	if object.Name != "fixture" || object.Namespace != "policies" || object.Spec.GetRules()[0].GetPolicy().GetActions()["github"].GetNames()[0] != "get_file_contents" {
		t.Fatalf("decoded = %+v", object)
	}
	encoded, err := json.Marshal(&object)
	if err != nil {
		t.Fatal(err)
	}
	var expected, actual map[string]any
	if err := json.Unmarshal([]byte(wrappedPolicy), &expected); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(encoded, &actual); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(expected["spec"], actual["spec"]) {
		t.Fatalf("spec changed: %s", encoded)
	}
	copy := object.DeepCopyObject().(*PolicySet)
	if !proto.Equal(object.Spec, copy.Spec) {
		t.Fatal("copy changed spec")
	}
	copy.Spec.Rules[0].Policy.Actions["github"].Names[0] = "changed"
	copy.Labels["owner"] = "changed"
	if object.Spec.Rules[0].Policy.Actions["github"].Names[0] != "get_file_contents" || object.Labels["owner"] != "test" {
		t.Fatal("copy shared mutable state")
	}
}

func TestRejectsUnknownSpecField(t *testing.T) {
	var object PolicySet
	err := json.Unmarshal([]byte(`{"spec":{"rules":[],"unknown":true}}`), &object)
	if err == nil {
		t.Fatal("unknown field accepted")
	}
}

// Struct stores JSON numbers as float64. This documents a real shape that cannot
// round-trip exactly; it is a limitation to resolve before using it for schemas.
func TestStructLargeIntegerPrecisionProbe(t *testing.T) {
	var object PolicySet
	input := `{"spec":{"rules":[{"type":"action_auto_approve","policy":{"type":"argument_schema","actions":{"g":{"names":["a"]}},"schema":{"maximum":9007199254740993}}}]}}`
	if err := json.Unmarshal([]byte(input), &object); err != nil {
		t.Fatal(err)
	}
	out, err := json.Marshal(&object)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(out), `"maximum":9007199254740992`) {
		t.Fatalf("precision probe changed, re-evaluate Struct suitability: %s", out)
	}
}
