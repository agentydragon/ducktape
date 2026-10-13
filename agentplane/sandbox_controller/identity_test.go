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
	if account.Labels[ActionCallerLabel] != "true" {
		t.Errorf("ServiceAccount labels = %v, want %s", account.Labels, ActionCallerLabel)
	}
}

func TestAccountCreatedWithoutTheActionCallerLabelGetsIt(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	var account corev1.ServiceAccount
	if err := c.Get(context.Background(), sandboxKey, &account); err != nil {
		t.Fatal(err)
	}
	delete(account.Labels, ActionCallerLabel)
	if err := c.Update(context.Background(), &account); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r)
	if err := c.Get(context.Background(), sandboxKey, &account); err != nil {
		t.Fatal(err)
	}
	if account.Labels[ActionCallerLabel] != "true" {
		t.Errorf("ServiceAccount labels = %v, want %s", account.Labels, ActionCallerLabel)
	}
}

func TestBindingIsCreatedWithThePodWithoutWaitingForItsReaders(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
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
	if getPod(t, c) == nil {
		t.Error("no Pod while the PolicyBinding's readers have not reported")
	}
}

func TestChangedPolicySetsUpdateTheBindingAndKeepTheRunningPod(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	setPolicySets(t, c, "test-set-a")
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

func TestSandboxOwnsAControlServiceBeforeAnyRunnerConnects(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	var service corev1.Service
	if err := c.Get(context.Background(), client.ObjectKey{Namespace: namespace, Name: sandboxName + ControlServiceSuffix}, &service); err != nil {
		t.Fatal(err)
	}
	if !metav1.IsControlledBy(&service, getSandbox(t, c)) {
		t.Errorf("Service owner references = %v", service.OwnerReferences)
	}
	if service.Spec.ClusterIP != corev1.ClusterIPNone || len(service.Spec.Selector) != 0 {
		t.Errorf("Service spec = %v, want headless without a selector", service.Spec)
	}
}

func TestControlServiceTheGatewayCreatedIsKept(t *testing.T) {
	created := &corev1.Service{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: sandboxName + ControlServiceSuffix,
		OwnerReferences: []metav1.OwnerReference{{APIVersion: "agentplane.allegedly.works/v1alpha1", Kind: "Sandbox", Name: sandboxName, UID: "uid-sandbox"}},
	}, Spec: corev1.ServiceSpec{ClusterIP: corev1.ClusterIPNone}}
	r, c := newReconciler(t, true, testTemplate("test-image:v1"), created)
	reconcileOnce(t, r)
	var service corev1.Service
	if err := c.Get(context.Background(), client.ObjectKeyFromObject(created), &service); err != nil {
		t.Fatal(err)
	}
	if len(service.Spec.Ports) != 0 {
		t.Errorf("Service ports = %v, want the gateway's Service untouched", service.Spec.Ports)
	}
}
