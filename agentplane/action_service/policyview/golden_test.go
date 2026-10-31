package policyview

import (
	_ "embed"
	"encoding/json"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/informer"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
)

// golden.json holds fixed policy inputs and their workload/operator API projections.
//
//go:embed golden.json
var goldenJSON []byte

// vector is a policy index, as the API server's objects, and what each reader is shown of the subject's policy.
type vector struct {
	PolicySets     []policyapi.PolicySet       `json:"policy_sets"`
	PolicyBindings []policyapi.PolicyBinding   `json:"policy_bindings"`
	Subject        policyapi.ServiceAccountRef `json:"subject"`
	Synced         bool                        `json:"synced"`
	Workload       json.RawMessage             `json:"workload"`
	Operator       json.RawMessage             `json:"operator"`
}

func TestReadersReceiveTheirPolicyViews(t *testing.T) {
	var golden struct {
		Views map[string]vector `json:"views"`
	}
	if err := json.Unmarshal(goldenJSON, &golden); err != nil {
		t.Fatal(err)
	}
	for name, v := range golden.Views {
		t.Run(name, func(t *testing.T) {
			index := informer.NewIndex(nil)
			for _, set := range v.PolicySets {
				index.Rules.PutPolicySet(&set)
			}
			for _, binding := range v.PolicyBindings {
				index.Rules.PutBinding(&binding)
			}
			if v.Synced {
				index.Health.MarkSynced()
			}
			for viewer, written := range map[Reader]json.RawMessage{Workload: v.Workload, Operator: v.Operator} {
				want := &action_servicepb.CallerActionPolicy{}
				if err := protojson.Unmarshal(written, want); err != nil {
					t.Fatal(err)
				}
				got := View(index, v.Subject, viewer)
				// A refusal's reason is its parser's own text: only that the set is refused is compared.
				for _, view := range []*action_servicepb.CallerActionPolicy{got, want} {
					for _, binding := range view.GetBindings() {
						for _, set := range binding.GetPolicySets() {
							if set.Refused != nil {
								set.Refused = proto.String("")
							}
						}
					}
				}
				if !proto.Equal(got, want) {
					t.Errorf("reader %d is shown\n%s\nwant\n%s", viewer, protojson.Format(got), protojson.Format(want))
				}
			}
		})
	}
}
