package sandboxcontroller

import (
	"context"
	"slices"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	policycontroller "github.com/agentydragon/ducktape/agentplane/policy_controller"
)

func setPolicySets(t *testing.T, c client.Client, sets ...string) {
	t.Helper()
	sandbox := getSandbox(t, c)
	sandbox.Spec.PolicySets = sets
	if err := c.Update(context.Background(), sandbox); err != nil {
		t.Fatal(err)
	}
}

func getBinding(t *testing.T, c client.Client) *policycontroller.PolicyBinding {
	t.Helper()
	var binding policycontroller.PolicyBinding
	if err := c.Get(context.Background(), sandboxKey, &binding); err != nil {
		if apierrors.IsNotFound(err) {
			return nil
		}
		t.Fatal(err)
	}
	return &binding
}

// report sets each reader's condition True for the binding's generation plus lag.
func report(t *testing.T, c client.Client, lag int64, readers ...string) {
	t.Helper()
	binding := getBinding(t, c)
	binding.Status.Conditions = nil
	for _, reader := range readers {
		binding.Status.Conditions = append(binding.Status.Conditions, metav1.Condition{
			Type: reader, Status: metav1.ConditionTrue, Reason: "Applied", ObservedGeneration: binding.Generation + lag,
			LastTransitionTime: metav1.Now(),
		})
	}
	if err := c.Status().Update(context.Background(), binding); err != nil {
		t.Fatal(err)
	}
}

// testSet is a PolicySet with one rule of each type.
func testSet(name string, ruleTypes ...string) *policycontroller.PolicySet {
	set := &policycontroller.PolicySet{ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: name}}
	for _, ruleType := range ruleTypes {
		set.Spec.Rules = append(set.Spec.Rules, policycontroller.Rule{Type: ruleType})
	}
	return set
}

func TestPodRunsAsTheSandboxsOwnServiceAccount(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	var account corev1.ServiceAccount
	if err := c.Get(context.Background(), sandboxKey, &account); err != nil {
		t.Fatal(err)
	}
	if !metav1.IsControlledBy(&account, getSandbox(t, c)) {
		t.Errorf("ServiceAccount owner references = %v", account.OwnerReferences)
	}
	if pod := getPod(t, c); pod == nil || pod.Spec.ServiceAccountName != sandboxName {
		t.Fatalf("Pod = %v, want one running as ServiceAccount %s", pod, sandboxName)
	}
	if getBinding(t, c) != nil {
		t.Error("a Sandbox naming no PolicySets got a PolicyBinding")
	}
}

func TestPodWaitsForTheReadersOfItsRuleTypes(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"),
		testSet("test-set-a", "kubernetes_role_binding"), testSet("test-set-b", "egress", "kubernetes_cluster_role_binding"))
	setPolicySets(t, c, "test-set-a", "test-set-b")
	reconcileOnce(t, r)

	binding := getBinding(t, c)
	if binding == nil || !metav1.IsControlledBy(binding, getSandbox(t, c)) {
		t.Fatalf("PolicyBinding = %v, want one the Sandbox controls", binding)
	}
	want := policycontroller.PolicyBindingSpec{
		Subject:    policycontroller.ServiceAccountRef{Namespace: namespace, Name: sandboxName},
		PolicySets: []string{"test-set-a", "test-set-b"},
	}
	if binding.Spec.Subject != want.Subject || !slices.Equal(binding.Spec.PolicySets, want.PolicySets) || binding.Spec.ExpiresAt != nil {
		t.Errorf("PolicyBinding spec = %+v, want %+v", binding.Spec, want)
	}

	for _, step := range []struct {
		name    string
		lag     int64
		readers []string
	}{
		{"no reader", 0, nil},
		{"one reader missing", 0, []string{policycontroller.KubernetesReady}},
		{"a stale generation", -1, []string{policycontroller.KubernetesReady, "EgressReady"}},
	} {
		if step.readers != nil {
			report(t, c, step.lag, step.readers...)
		}
		reconcileOnce(t, r)
		if getPod(t, c) != nil {
			t.Fatalf("%s: a Pod started before every reader reported the current generation", step.name)
		}
		if reason := runningReason(t, c); reason != "PolicyBindingNotReady" {
			t.Errorf("%s: Running reason = %s, want PolicyBindingNotReady", step.name, reason)
		}
	}

	// No rule is the Action Service's, so its condition is never awaited.
	report(t, c, 0, policycontroller.KubernetesReady, "EgressReady")
	reconcileOnce(t, r)
	if getPod(t, c) == nil {
		t.Fatal("no Pod once the readers of its rule types reported")
	}
}

func TestMissingPolicySetBlocksThePod(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	setPolicySets(t, c, "test-set-missing")
	reconcileOnce(t, r)
	report(t, c, 0, policycontroller.KubernetesReady, "EgressReady", "ActionsReady")
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("a Pod started without knowing the rules of a PolicySet it names")
	}
	if reason := runningReason(t, c); reason != "PolicySetNotFound" {
		t.Errorf("Running reason = %s, want PolicySetNotFound", reason)
	}
}

func TestChangedPolicySetsUpdateTheBindingAndKeepTheRunningPod(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"), testSet("test-set-a", "egress"), testSet("test-set-b", "egress"))
	setPolicySets(t, c, "test-set-a")
	reconcileOnce(t, r)
	report(t, c, 0, "EgressReady")
	reconcileOnce(t, r)
	if getPod(t, c) == nil {
		t.Fatal("no Pod")
	}

	setPolicySets(t, c, "test-set-b")
	reconcileOnce(t, r)
	if sets := getBinding(t, c).Spec.PolicySets; !slices.Equal(sets, []string{"test-set-b"}) {
		t.Errorf("PolicyBinding sets = %v, want the new spec's", sets)
	}
	if getPod(t, c) == nil {
		t.Error("changing the PolicySets stopped the running Pod")
	}

	setPolicySets(t, c)
	reconcileOnce(t, r)
	if getBinding(t, c) != nil {
		t.Error("clearing the PolicySets left the PolicyBinding")
	}
}

func TestStopIsNotGatedOnTheBinding(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	setPolicySets(t, c, "test-set-a")
	reconcileOnce(t, r)
	setRunning(t, c, false)
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("a stopped Sandbox kept its Pod while its PolicyBinding was not ready")
	}
}

func TestForeignServiceAccountBlocksThePod(t *testing.T) {
	foreign := &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: sandboxName}}
	r, c := newReconciler(t, true, testTemplate("test-image:v1"), foreign)
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("a Pod started as a ServiceAccount the Sandbox does not own")
	}
	if reason := runningReason(t, c); reason != "ServiceAccountConflict" {
		t.Errorf("Running reason = %s, want ServiceAccountConflict", reason)
	}
}
