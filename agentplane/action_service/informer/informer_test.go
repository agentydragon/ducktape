package informer

import (
	"context"
	"fmt"
	"slices"
	"strings"
	"testing"
	"time"

	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"

	"github.com/agentydragon/ducktape/agentplane/action_service/policies"
	"github.com/agentydragon/ducktape/agentplane/action_service/store"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/go/auth"
)

var testAction = store.ActionIdentity{Group: "test-group", Name: "test-action"}

func exactActions() policyapi.Rule {
	return policyapi.Rule{Type: policies.RuleType, Policy: &policyapi.ActionPolicy{
		Type: string(store.PolicyExactActions), Actions: map[string]policyapi.ActionNames{testAction.Group: {testAction.Name}},
	}}
}

// otherReaders is a rule another reader enforces, which this one skips but numbers its own rules around.
func otherReaders() policyapi.Rule {
	return policyapi.Rule{Type: "egress", Hosts: []string{"informer-test.invalid"}}
}

// refusedSchema is an argument_schema rule the CRD admits and this service refuses: its schema is no JSON Schema.
func refusedSchema() policyapi.Rule {
	return policyapi.Rule{Type: policies.RuleType, Policy: &policyapi.ActionPolicy{
		Type:    string(store.PolicyArgumentSchema),
		Actions: map[string]policyapi.ActionNames{testAction.Group: {testAction.Name}},
		Schema:  &runtime.RawExtension{Raw: []byte(`{"type": 5}`)},
	}}
}

func TestOnlyLabeledServiceAccountsOfTheCallerNamespacesAreAdmitted(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	admitted := auth.Account{Namespace: f.callers, Name: "test-labeled"}
	labeled := f.serviceAccount(admitted, "true")
	unlabeled := auth.Account{Namespace: f.callers, Name: "test-unlabeled"}
	becomesLabeled := f.serviceAccount(unlabeled, "")
	alsoAdmitted := auth.Account{Namespace: f.moreCallers, Name: "test-labeled"}
	deleted := f.serviceAccount(alsoAdmitted, "true")
	notTrue := auth.Account{Namespace: f.callers, Name: "test-labeled-false"}
	f.serviceAccount(notTrue, "false")
	outside := auth.Account{Namespace: f.elsewhere, Name: "test-labeled"}
	f.serviceAccount(outside, "true")
	f.start()

	f.eventually("the index to sync", f.available)
	f.eventually("the labeled accounts to be admitted", func() bool { return f.index.Admits(admitted) && f.index.Admits(alsoAdmitted) })
	for _, refused := range []auth.Account{unlabeled, notTrue, outside} {
		if f.index.Admits(refused) {
			t.Errorf("%+v is admitted", refused)
		}
	}
	// f.callers sorts before f.moreCallers.
	if listed, available := f.index.CallerServiceAccounts(); !available || !slices.Equal(listed, []auth.Account{admitted, alsoAdmitted}) {
		t.Errorf("the callers listed: %v, %v", listed, available)
	}

	delete(labeled.Labels, CallerLabel)
	f.update(labeled)
	becomesLabeled.Labels = map[string]string{CallerLabel: "true"}
	f.update(becomesLabeled)
	f.remove(deleted)
	f.eventually("the label changes and the deletion to be followed", func() bool {
		return !f.index.Admits(admitted) && f.index.Admits(unlabeled) && !f.index.Admits(alsoAdmitted)
	})
	if listed, available := f.index.CallerServiceAccounts(); !available || !slices.Equal(listed, []auth.Account{unlabeled}) {
		t.Errorf("the callers listed after the changes: %v, %v", listed, available)
	}
}

func TestNothingIsAdmittedOrResolvedWhileACallerNamespaceFails(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	caller := auth.Account{Namespace: f.moreCallers, Name: "test-workload"}
	f.serviceAccount(caller, "true")
	subject := policyapi.ServiceAccountRef{Namespace: caller.Namespace, Name: caller.Name}
	f.create(f.policySet("test-set", exactActions()))
	f.create(f.policyBinding(f.policy, "test-grant", subject, "test-set"))
	f.start()

	f.eventually("the index to sync", f.available)
	if !f.index.Admits(caller) || len(f.index.ResolveBindings(subject)) != 1 {
		t.Fatal("a synced index does not admit the caller or resolve its binding")
	}

	// The requests of one caller namespace fail while every other kind's keep succeeding.
	restore := f.faults.fail("/api/v1/namespaces/" + f.callers + "/serviceaccounts")
	f.eventually("the index to turn unavailable", func() bool { return !f.available() })
	status := f.index.Health.Status()
	if _, failing := status.Failing[serviceAccounts(f.callers)]; !failing || len(status.Failing) != 1 {
		t.Errorf("unavailable for something else than the failing namespace: %+v", status)
	}
	if listed, available := f.index.CallerServiceAccounts(); f.index.Admits(caller) || f.index.ResolveBindings(subject) != nil || available || listed != nil {
		t.Error("an unavailable index admits a caller, resolves its bindings or lists the callers")
	}

	restore()
	f.eventually("the index to recover", f.available)
	if !f.index.Admits(caller) {
		t.Error("the recovered index does not admit the caller")
	}
}

func TestBindingsResolveAsAdmissionDecidesFromThem(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	subject := policyapi.ServiceAccountRef{Namespace: f.callers, Name: "test-workload"}
	f.create(f.policySet("test-auto", otherReaders(), exactActions()))
	f.create(f.policySet("test-other-reader", otherReaders()))
	f.create(f.policySet("test-refused", refusedSchema()))
	f.create(f.policyBinding(f.policy, "test-b", subject, "test-refused", "test-auto", "test-missing"))
	f.create(f.policyBinding(f.policy, "test-a", subject, "test-other-reader"))
	f.create(f.policyBinding(f.policy, "test-someone-else", policyapi.ServiceAccountRef{Namespace: f.callers, Name: "test-other"}, "test-auto"))
	f.start()

	// Once ActionsReady is written on both, the bindings stay as they are.
	for _, name := range []string{"test-a", "test-b"} {
		f.eventuallyActionsReady(name, func(*metav1.Condition) bool { return true })
	}
	auto, other := f.set("test-auto"), f.set("test-other-reader")
	var a, b *policyapi.PolicyBinding
	var got, want []string
	f.eventually("the bindings to resolve as the API server holds them", func() bool {
		a, b = f.binding(f.policy, "test-a"), f.binding(f.policy, "test-b")
		want = []string{
			// A set of another reader's rules grants none of this one's, and still stands as named.
			fmt.Sprintf("%s/test-a@%s: %s/test-other-reader@%d %v", f.policy, a.ResourceVersion, f.policy, other.Generation, []int(nil)),
			// The refused and the missing set contribute nothing; the auto-approve rule keeps its number in its set.
			fmt.Sprintf("%s/test-b@%s: %s/test-auto@%d %v", f.policy, b.ResourceVersion, f.policy, auto.Generation, []int{1}),
		}
		got = resolved(f.index.ResolveBindings(subject))
		return slices.Equal(got, want)
	}, func() string { return fmt.Sprintf("ResolveBindings = %q, want %q", got, want) })

	visibility, err := policies.NewVisibility("http://github.informer-test.invalid", time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	allowed, ok := policies.NewProvider(visibility).Decide(context.Background(), f.index.ResolveBindings(subject), testAction, map[string]any{}).(policies.Allow)
	if !ok {
		t.Fatal("the provider does not allow the Action a resolved binding auto-approves")
	}
	evidence := allowed.Vote.Evidence
	if len(evidence.Bindings) != 2 || evidence.Bindings[1].ResourceVersion != b.ResourceVersion ||
		evidence.Matched != (store.MatchedPolicy{
			Namespace: f.policy, PolicySet: "test-auto", Source: store.PolicySourceRules, Index: 1, Type: store.PolicyExactActions,
		}) {
		t.Errorf("evidence = %+v", evidence)
	}
}

func TestActionsReadyIsAppliedOnlyOnThePolicyNamespacesBindings(t *testing.T) {
	t.Parallel()
	f := newFixture(t)
	subject := policyapi.ServiceAccountRef{Namespace: f.callers, Name: "test-workload"}
	f.create(f.policySet("test-auto", exactActions()))
	f.create(f.policySet("test-refused", refusedSchema()))
	f.create(f.policyBinding(f.policy, "test-ready", subject, "test-auto"))
	f.create(f.policyBinding(f.policy, "test-unready", subject, "test-auto", "test-refused"))
	// A binding of the same name outside the policy namespace, which this service does not judge.
	f.create(f.policyBinding(f.elsewhere, "test-ready", subject, "test-auto"))
	f.start()

	// The reason and field manager are the Python service's (policy_informer.py, policies/resources.py): while one
	// service replaces the other, both apply this condition and must own it as one writer.
	ready := f.eventuallyActionsReady("test-ready", func(*metav1.Condition) bool { return true })
	if ready.Status != metav1.ConditionTrue || ready.Reason != "Applied" {
		t.Errorf("a binding whose sets all parse: %+v", ready)
	}
	written := f.binding(f.policy, "test-ready")
	if !slices.ContainsFunc(written.ManagedFields, func(entry metav1.ManagedFieldsEntry) bool {
		return entry.Manager == "agentplane-action-service" && entry.Operation == metav1.ManagedFieldsOperationApply && entry.Subresource == "status"
	}) {
		t.Errorf("ActionsReady was not applied by agentplane-action-service: %+v", written.ManagedFields)
	}
	unready := f.eventuallyActionsReady("test-unready", func(*metav1.Condition) bool { return true })
	if unready.Status != metav1.ConditionFalse || unready.Reason != policyapi.PolicySetUnavailable {
		t.Errorf("a binding naming a refused set: %+v", unready)
	}
	if elsewhere := f.binding(f.elsewhere, "test-ready"); meta.FindStatusCondition(elsewhere.Status.Conditions, ActionsReady.Type) != nil {
		t.Errorf("a binding outside the policy namespace was judged: %+v", elsewhere.Status.Conditions)
	}
}

// resolved is each binding as `namespace/name@resourceVersion: ` and its sets, each as
// `namespace/name@generation [rule numbers]`.
func resolved(bindings []policies.Binding) []string {
	var described []string
	for _, binding := range bindings {
		var sets []string
		for _, set := range binding.PolicySets {
			var numbers []int
			for _, numbered := range set.Policies {
				numbers = append(numbers, numbered.Number)
			}
			sets = append(sets, fmt.Sprintf("%s/%s@%d %v", set.Namespace, set.Name, set.Generation, numbers))
		}
		described = append(described, fmt.Sprintf("%s/%s@%s: %s", binding.Namespace, binding.Name, binding.ResourceVersion, strings.Join(sets, ", ")))
	}
	return described
}
