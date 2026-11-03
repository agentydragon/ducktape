// Package codec experiments with a Kubernetes wrapper around a generated protobuf spec.
// It is not used by the production Policy Controller.
package codec

import (
	"encoding/json"
	"fmt"

	pb "github.com/agentydragon/ducktape/agentplane/policy/protofirst"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
)

type PolicySet struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec              *pb.PolicySet `json:"spec"`
}

type wirePolicySet struct {
	APIVersion string            `json:"apiVersion,omitempty"`
	Kind       string            `json:"kind,omitempty"`
	Metadata   metav1.ObjectMeta `json:"metadata,omitempty"`
	Spec       json.RawMessage   `json:"spec"`
}

func (p PolicySet) MarshalJSON() ([]byte, error) {
	if p.Spec == nil {
		return nil, fmt.Errorf("missing PolicySet spec")
	}
	spec, err := protojson.Marshal(p.Spec)
	if err != nil {
		return nil, fmt.Errorf("encode PolicySet spec: %w", err)
	}
	return json.Marshal(wirePolicySet{APIVersion: p.APIVersion, Kind: p.Kind, Metadata: p.ObjectMeta, Spec: spec})
}

func (p *PolicySet) UnmarshalJSON(data []byte) error {
	var wire wirePolicySet
	if err := json.Unmarshal(data, &wire); err != nil {
		return err
	}
	if len(wire.Spec) == 0 || string(wire.Spec) == "null" {
		return fmt.Errorf("missing PolicySet spec")
	}
	spec := &pb.PolicySet{}
	if err := protojson.Unmarshal(wire.Spec, spec); err != nil {
		return fmt.Errorf("decode PolicySet spec: %w", err)
	}
	p.TypeMeta = metav1.TypeMeta{APIVersion: wire.APIVersion, Kind: wire.Kind}
	p.ObjectMeta = wire.Metadata
	p.Spec = spec
	return nil
}

func (p *PolicySet) DeepCopyObject() runtime.Object {
	if p == nil {
		return nil
	}
	out := *p
	out.ObjectMeta = *p.ObjectMeta.DeepCopy()
	if p.Spec != nil {
		out.Spec = proto.Clone(p.Spec).(*pb.PolicySet)
	}
	return &out
}
