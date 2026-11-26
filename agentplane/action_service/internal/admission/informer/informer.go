// Package informer keeps the Action Service's policy Index equal to the API server: the `action_auto_approve` rules of
// the policy namespace's PolicySets, the PolicyBindings that grant them, and its Identities. Its one write is the
// ActionsReady condition on each PolicyBinding of the policy namespace.
package informer

import (
	"context"
	"log/slog"

	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/rest"

	"github.com/agentydragon/ducktape/agentplane/action_service/internal/admission/policies"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	"github.com/agentydragon/ducktape/agentplane/watchhealth"
)

// ActionsReady is the condition this service keeps on each PolicyBinding of the policy namespace.
var ActionsReady = reader.Condition{
	Type: "ActionsReady", FieldManager: "agentplane-action-service", Rules: "Action rules", Reason: "Applied",
}

// Index is what the Action Service reads of the API server to admit a caller and decide its requests. Its writer is
// the Informer; readers take the state of one moment.
type Index struct {
	Rules *reader.Index[policies.Policy]
	// Health covers the policy namespace's PolicySets, PolicyBindings and Identities.
	Health *watchhealth.Health
}

// NewIndex returns an empty Index. It is unavailable until an Informer has listed every kind, and while any kind fails
// to list or watch.
func NewIndex() *Index {
	return &Index{Rules: reader.NewIndex(policies.RuleType, policies.ParseRule), Health: watchhealth.New()}
}

// AdmitsIdentity reports whether an Identity incarnation may call the service: a live PolicyBinding of the policy
// namespace names it now, which the Index answers only while that Identity of that UID exists. Nothing is admitted
// while the Index is unavailable, so a replica that cannot reach the API server refuses every caller rather than serve
// a picture it cannot vouch for. Every refusal is logged.
func (i *Index) AdmitsIdentity(identity auth.Identity) bool {
	if !i.Health.Available() {
		slog.Warn("workload SVID refused: the policy index is unavailable", "identity", identity.Name, "uid", identity.UID)
		return false
	}
	if len(i.Rules.Bindings(reader.Identity{Name: identity.Name, UID: types.UID(identity.UID)})) == 0 {
		slog.Warn("workload SVID refused: no live PolicyBinding names the Identity", "identity", identity.Name, "uid", identity.UID)
		return false
	}
	return true
}

// Resolve is the subject's PolicyBindings in name order, each with how the sets it names stand, and whether the Index
// was available to read them: nothing when it was not. The Policy Controller deletes a binding at expiry. Admission
// and the policy views read this one resolution, so what a view reports and what admission decides cannot drift.
func (i *Index) Resolve(subject reader.Subject) ([]reader.BindingResolution[policies.Policy], bool) {
	if !i.Health.Available() {
		return nil, false
	}
	return i.Rules.Bindings(subject), true
}

// ResolveBindings is the caller's PolicyBindings as admission decides from them, in name order, each with the sets it
// names that exist and parse; a missing or refused set contributes nothing. Nothing while the Index is unavailable, so
// every request then waits for an operator.
func (i *Index) ResolveBindings(caller reader.Subject) []policies.Binding {
	resolved, _ := i.Resolve(caller)
	var bindings []policies.Binding
	for _, bound := range resolved {
		binding := policies.Binding{
			Namespace: bound.Binding.Namespace, Name: bound.Binding.Name, ResourceVersion: bound.Binding.ResourceVersion,
		}
		for _, set := range bound.Sets {
			binding.PolicySets = append(binding.PolicySets, policies.PolicySet{
				Namespace: bound.Binding.Namespace, Name: set.Name, Generation: set.Generation, Policies: set.Rules,
			})
		}
		bindings = append(bindings, binding)
	}
	return bindings
}

// Informer fills an Index from the API server and keeps ActionsReady on the policy namespace's bindings.
type Informer struct{ watches *reader.Watches }

// New returns an Informer of the Index that is idle until Run. policyNamespace holds the PolicySets and PolicyBindings;
// identities is where the Index follows their subjects' Identities.
func New(base *rest.Config, index *Index, policyNamespace string, identities reader.IdentitySource) (*Informer, error) {
	watches := reader.NewWatches(base, index.Health)
	if err := reader.WatchRules(watches, index.Rules, policyNamespace, ActionsReady, identities); err != nil {
		return nil, err
	}
	return &Informer{watches: watches}, nil
}

// Run lists and watches until ctx is done; see reader.Watches.Run.
func (i *Informer) Run(ctx context.Context) { i.watches.Run(ctx) }
