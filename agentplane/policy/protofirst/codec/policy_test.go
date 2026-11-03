package codec

import (
	"encoding/json"
	"reflect"
	"testing"

	"google.golang.org/protobuf/proto"
)

const existingPolicy = `{"apiVersion":"agentplane.allegedly.works/v1alpha1","kind":"PolicySet","metadata":{"name":"fixture","namespace":"policies","labels":{"owner":"test"}},"spec":{"rules":[{"type":"action_auto_approve","policy":{"type":"exact_actions","actions":{"github":["get_file_contents","list_commits"]}}}]}}`

func TestExistingPolicyJSONAndDeepCopy(t *testing.T) {
	var object PolicySet
	if err := json.Unmarshal([]byte(existingPolicy), &object); err != nil {
		t.Fatal(err)
	}
	if object.Name != "fixture" || object.Namespace != "policies" || object.Spec.GetRules()[0].GetPolicy().GetActions()["github"].GetValues()[0].GetStringValue() != "get_file_contents" {
		t.Fatalf("decoded = %+v", object)
	}
	encoded, err := json.Marshal(&object)
	if err != nil {
		t.Fatal(err)
	}
	var expected, actual map[string]any
	if err := json.Unmarshal([]byte(existingPolicy), &expected); err != nil {
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
	copy.Spec.Rules[0].Policy.Actions["github"].Values[0].Kind = nil
	copy.Labels["owner"] = "changed"
	if object.Spec.Rules[0].Policy.Actions["github"].Values[0].GetStringValue() != "get_file_contents" || object.Labels["owner"] != "test" {
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
