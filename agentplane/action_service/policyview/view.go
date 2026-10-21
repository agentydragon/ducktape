// Package policyview is what a reader may know of an account's Action policy: what its bindings auto-decide, as
// admission would resolve them now. A caller cannot act on policy it cannot read: without this it would have to submit
// and watch the Decision to learn whether an Action waits for an operator.
//
// A workload's view is redacted by construction: it is built from its own field list, never from a resource wholesale.
// A set that is missing or refused contributes nothing and simply is not there; the binding diagnostics are the
// operator's to read.
package policyview

import (
	"cmp"
	"fmt"
	"slices"

	"google.golang.org/protobuf/types/known/timestamppb"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/sets"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	"github.com/agentydragon/ducktape/agentplane/action_service/informer"
	"github.com/agentydragon/ducktape/agentplane/action_service/policies"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	sandboxespb "github.com/agentydragon/ducktape/agentplane/proto/sandboxes"
)

// Reader is who reads a view.
type Reader int

const (
	// Workload is a caller: it is shown each binding's sets that resolved, present and valid.
	Workload Reader = iota + 1
	// Operator is also shown each binding's diagnostics: its labels, generation and ActionsReady, the sets it names that
	// are refused, and the names no set answers to.
	Operator
)

// View is the subject's effective Action policy as reader may see it, from the resolution admission decides from.
// While the index is unavailable it holds no binding and no policy, as nothing then auto-decides.
func View(index *informer.Index, subject policyapi.ServiceAccountRef, viewer Reader) *action_servicepb.CallerActionPolicy {
	resolved, synced := index.Resolve(subject)
	view := &action_servicepb.CallerActionPolicy{
		Subject: &sandboxespb.ServiceAccount{Namespace: subject.Namespace, Name: subject.Name},
		Synced:  synced,
	}
	approvable := sets.New[store.ActionIdentity]()
	for _, bound := range resolved {
		view.Bindings = append(view.Bindings, binding(bound, viewer))
		// In the order the provider walks them: binding, then set, then rule.
		for _, set := range bound.Sets {
			for _, numbered := range set.Rules {
				view.AutoApproveIf = append(view.AutoApproveIf, &action_servicepb.EffectivePolicy{
					Binding: bound.Binding.Name, PolicySet: set.Name, Source: action_servicepb.PolicySource_POLICY_SOURCE_RULES,
					Index: int32(numbered.Number), Policy: policy(numbered.Rule),
				})
				for group, names := range numbered.Rule.Actions {
					for name := range names {
						approvable.Insert(store.ActionIdentity{Group: group, Name: name})
					}
				}
			}
		}
	}
	sorted := approvable.UnsortedList()
	slices.SortFunc(sorted, func(a, b store.ActionIdentity) int {
		return cmp.Or(cmp.Compare(a.Group, b.Group), cmp.Compare(a.Name, b.Name))
	})
	for _, action := range sorted {
		view.AutoApprovable = append(view.AutoApprovable, &action_servicepb.ActionIdentity{Group: action.Group, Name: action.Name})
	}
	return view
}

func binding(bound reader.BindingResolution[policies.Policy], viewer Reader) *action_servicepb.CallerBinding {
	view := &action_servicepb.CallerBinding{Name: bound.Binding.Name}
	if expiresAt := bound.Binding.Spec.ExpiresAt; expiresAt != nil {
		view.ExpiresAt = timestamppb.New(expiresAt.Time)
	}
	if viewer == Workload {
		for _, set := range bound.Sets {
			view.PolicySets = append(view.PolicySets, &action_servicepb.ActionPolicySet{Name: set.Name, Generation: set.Generation})
		}
		return view
	}
	valid := map[string]reader.PolicySet[policies.Policy]{}
	for _, set := range bound.Sets {
		valid[set.Name] = set
	}
	refused := map[string]reader.Refusal{}
	for _, refusal := range bound.Refused {
		refused[refusal.Set] = refusal
	}
	// Valid and refused sets in the binding's order.
	for _, name := range bound.Binding.Spec.PolicySets {
		if set, ok := valid[name]; ok {
			view.PolicySets = append(view.PolicySets, &action_servicepb.ActionPolicySet{Name: name, Generation: set.Generation})
		} else if refusal, ok := refused[name]; ok {
			view.PolicySets = append(view.PolicySets, &action_servicepb.ActionPolicySet{
				Name: name, Generation: refusal.Generation, Refused: &refusal.Reason,
			})
		}
	}
	view.MissingPolicySets = bound.Missing
	view.Labels = bound.Binding.Labels
	view.Generation = &bound.Binding.Generation
	if condition := meta.FindStatusCondition(bound.Binding.Status.Conditions, informer.ActionsReady.Type); condition != nil {
		view.Ready = &action_servicepb.ReadyCondition{
			Status: conditionStatus(condition.Status), Reason: condition.Reason, Message: condition.Message,
		}
		// A generation is at least 1, so 0 is a condition that names none.
		if condition.ObservedGeneration != 0 {
			view.Ready.ObservedGeneration = &condition.ObservedGeneration
		}
	}
	return view
}

// conditionStatus maps a condition's status; the CRD admits only True, False and Unknown.
func conditionStatus(status metav1.ConditionStatus) action_servicepb.ConditionStatus {
	switch status {
	case metav1.ConditionTrue:
		return action_servicepb.ConditionStatus_CONDITION_STATUS_TRUE
	case metav1.ConditionFalse:
		return action_servicepb.ConditionStatus_CONDITION_STATUS_FALSE
	default:
		return action_servicepb.ConditionStatus_CONDITION_STATUS_UNKNOWN
	}
}

func policy(rule policies.Policy) *action_servicepb.Policy {
	actions := stringLists(rule.Actions)
	switch kind := rule.Kind.(type) {
	case policies.ExactActions:
		return &action_servicepb.Policy{Kind: &action_servicepb.Policy_ExactActions{
			ExactActions: &action_servicepb.ExactActionsPolicy{Actions: actions},
		}}
	case policies.ArgumentSchema:
		return &action_servicepb.Policy{Kind: &action_servicepb.Policy_ArgumentSchema{
			ArgumentSchema: &action_servicepb.ArgumentSchemaPolicy{Actions: actions, ArgumentSchema: kind.Schema},
		}}
	case policies.GitHubRepository:
		return &action_servicepb.Policy{Kind: &action_servicepb.Policy_GithubRepository{
			GithubRepository: &action_servicepb.GitHubRepositoryPolicy{Actions: actions, Owner: kind.Owner, Repository: kind.Repository},
		}}
	case policies.GitHubPublicRepository:
		return &action_servicepb.Policy{Kind: &action_servicepb.Policy_GithubPublicRepository{
			GithubPublicRepository: &action_servicepb.GitHubPublicRepositoryPolicy{Actions: actions},
		}}
	case policies.HomeAssistantEntityControl:
		return &action_servicepb.Policy{Kind: &action_servicepb.Policy_HomeAssistantEntityControl{
			HomeAssistantEntityControl: &action_servicepb.HomeAssistantEntityControlPolicy{
				Actions: actions, Entities: stringLists(kind.Entities),
			},
		}}
	default:
		// Every kind is one of the above: Kind's evaluate is unexported.
		panic(fmt.Sprintf("policy kind %T has no view", kind))
	}
}

// stringLists is each key's names, sorted.
func stringLists(named map[string]sets.Set[string]) map[string]*action_servicepb.StringList {
	lists := make(map[string]*action_servicepb.StringList, len(named))
	for key, names := range named {
		lists[key] = &action_servicepb.StringList{Values: sets.List(names)}
	}
	return lists
}
