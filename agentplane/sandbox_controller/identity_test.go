package sandboxcontroller

import (
	"context"
	"testing"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	workloadapi "github.com/agentydragon/ducktape/agentplane/workload_identity/api/v1alpha1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/equality"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
)

func getBinding(t *testing.T, c client.Client) *policyapi.PolicyBinding {
	t.Helper()
	var binding policyapi.PolicyBinding
	if err := c.Get(context.Background(), sandboxKey, &binding); err != nil {
		if apierrors.IsNotFound(err) {
			return nil
		}
		t.Fatal(err)
	}
	return &binding
}

func TestSandboxOwnsAControlServiceBeforeAnyRunnerConnects(t *testing.T) {
	r, c := newReconciler(t, true, testTemplate("test-image:v1"))
	reconcileOnce(t, r)
	var service corev1.Service
	if err := c.Get(context.Background(), client.ObjectKey{Namespace: namespace, Name: sandboxName + sandboxapi.ControlServiceSuffix}, &service); err != nil {
		t.Fatal(err)
	}
	if !metav1.IsControlledBy(&service, getSandbox(t, c)) {
		t.Errorf("Service owner references = %v", service.OwnerReferences)
	}
	if service.Spec.ClusterIP != corev1.ClusterIPNone || len(service.Spec.Selector) != 0 {
		t.Errorf("Service spec = %v, want headless without a selector", service.Spec)
	}
}

func TestControlServiceTheGatewayCreatedIsReconciled(t *testing.T) {
	created := &corev1.Service{ObjectMeta: metav1.ObjectMeta{
		Namespace: namespace, Name: sandboxName + sandboxapi.ControlServiceSuffix,
		OwnerReferences: []metav1.OwnerReference{{APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind, Name: sandboxName, UID: "uid-sandbox"}},
	}, Spec: corev1.ServiceSpec{ClusterIP: corev1.ClusterIPNone}}
	r, c := newReconciler(t, true, testTemplate("test-image:v1"), created)
	reconcileOnce(t, r)
	var service corev1.Service
	if err := c.Get(context.Background(), client.ObjectKeyFromObject(created), &service); err != nil {
		t.Fatal(err)
	}
	if len(service.Spec.Ports) != 1 || service.Spec.Ports[0].Port != sandboxapi.ControlServicePort {
		t.Errorf("Service ports = %v, want the control port", service.Spec.Ports)
	}
}

const testIdentityUID = "00000000-0000-4000-8000-00000000000a"

func testIdentity(name string, uid types.UID) *policyapi.Identity {
	return &policyapi.Identity{
		ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: name, UID: uid},
		Spec:       policyapi.IdentitySpec{Description: "test identity"},
	}
}

// newIdentityReconciler is a controller in identity mode over a running Sandbox naming identity,
// or no identity when it is empty.
func newIdentityReconciler(t *testing.T, identity string, objects ...client.Object) (*Reconciler, client.Client) {
	t.Helper()
	r, c := newReconciler(t, true, append(objects, testTemplate("test-image:v1"))...)
	sandbox := getSandbox(t, c)
	sandbox.Spec.Identity = nil
	if identity != "" {
		sandbox.Spec.Identity = &sandboxapi.IdentityRef{Name: identity}
	}
	if err := c.Update(context.Background(), sandbox); err != nil {
		t.Fatal(err)
	}
	return r, c
}

func setIdentity(t *testing.T, c client.Client, name string) {
	t.Helper()
	sandbox := getSandbox(t, c)
	sandbox.Spec.Identity = &sandboxapi.IdentityRef{Name: name}
	if err := c.Update(context.Background(), sandbox); err != nil {
		t.Fatal(err)
	}
}

// assertNoAccountOrBinding checks the Sandbox made no ServiceAccount in any namespace, the accounts
// namespace included, and no PolicyBinding of its own.
func assertNoAccountOrBinding(t *testing.T, c client.Client) {
	t.Helper()
	var accounts corev1.ServiceAccountList
	if err := c.List(context.Background(), &accounts); err != nil {
		t.Fatal(err)
	}
	if len(accounts.Items) != 0 {
		t.Errorf("ServiceAccounts = %v, want none", accounts.Items)
	}
	if binding := getBinding(t, c); binding != nil {
		t.Errorf("PolicyBinding = %+v, want none", binding.Spec)
	}
}

func recordSubject(t *testing.T, c client.Client) workloadapi.Subject {
	t.Helper()
	return readRecord(t, c, getSandbox(t, c)).Spec.Subject
}

func TestASandboxActingAsNoLiveIdentityStartsNothingAndCreatesNothing(t *testing.T) {
	for name, identity := range map[string]string{"unbound": "", "naming a missing Identity": "missing-identity"} {
		t.Run(name, func(t *testing.T) {
			r, c := newIdentityReconciler(t, identity)
			reconcileOnce(t, r)
			if pod := getPod(t, c); pod != nil {
				t.Fatalf("Pod %s started with no live identity", pod.Name)
			}
			assertNoAccountOrBinding(t, c)
			if err := c.Get(context.Background(), sandboxKey, &workloadapi.WorkloadCredential{}); !apierrors.IsNotFound(err) {
				t.Errorf("WorkloadCredential: %v, want none while nothing may start", err)
			}
			sandbox := getSandbox(t, c)
			if reason := runningReason(t, c); reason != "IdentityMissing" {
				t.Errorf("Running reason = %s, want IdentityMissing", reason)
			}
			// Nothing runs, which is what admission reads to let the Sandbox be bound.
			if sandbox.Status.StopStage != sandboxapi.StopStageStopped {
				t.Errorf("stopStage = %q, want Stopped", sandbox.Status.StopStage)
			}
		})
	}
}

func TestASandboxActingAsALiveIdentityRunsOnTheDefaultAccountWithItsCredential(t *testing.T) {
	r, c := newIdentityReconciler(t, "", testIdentity("test-agent", testIdentityUID))
	reconcileOnce(t, r)
	if getPod(t, c) != nil {
		t.Fatal("a Pod started before the Sandbox named its identity")
	}

	// Binding an unbound environment starts it; the Identity watch is what wakes it in a manager.
	setIdentity(t, c, "test-agent")
	reconcileOnce(t, r)
	pod := getPod(t, c)
	if pod == nil {
		t.Fatalf("no Pod: %s", runningReason(t, c))
	}
	if pod.Spec.ServiceAccountName != "default" || pod.Spec.AutomountServiceAccountToken == nil || *pod.Spec.AutomountServiceAccountToken {
		t.Errorf("Pod runs as %q with automount %v, want the default account with no token mounted",
			pod.Spec.ServiceAccountName, pod.Spec.AutomountServiceAccountToken)
	}
	assertNoAccountOrBinding(t, c)
	want := workloadapi.Subject{Identity: &workloadapi.IdentitySubject{Name: "test-agent", UID: testIdentityUID}}
	if subject := recordSubject(t, c); !equality.Semantic.DeepEqual(subject, want) {
		t.Errorf("WorkloadCredential subject = %+v, want %+v", subject, want)
	}
}

func TestARunningPodOutlivesItsDeletedIdentityButNoNextStartDoes(t *testing.T) {
	identity := testIdentity("test-agent", testIdentityUID)
	r, c := newIdentityReconciler(t, "test-agent", identity)
	reconcileOnce(t, r)
	pod := getPod(t, c)
	if pod == nil {
		t.Fatalf("no Pod: %s", runningReason(t, c))
	}

	if err := c.Delete(context.Background(), identity); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r)
	if current := getPod(t, c); current == nil || current.UID != pod.UID {
		t.Fatalf("Pod = %v, want the running incarnation kept", current)
	}
	if subject := recordSubject(t, c); subject.Identity == nil || subject.Identity.UID != testIdentityUID {
		t.Errorf("WorkloadCredential subject = %+v, want the incarnation's own", subject)
	}

	if err := c.Delete(context.Background(), pod); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r)
	if replacement := getPod(t, c); replacement != nil {
		t.Errorf("Pod %s replaced the ended one with no live identity", replacement.Name)
	}
	if reason := runningReason(t, c); reason != "IdentityMissing" {
		t.Errorf("Running reason = %s, want IdentityMissing", reason)
	}
}

func TestAPodActingAsAnIdentityItsSandboxNoLongerNamesIsReplaced(t *testing.T) {
	const otherUID = "00000000-0000-4000-8000-00000000000b"
	r, c := newIdentityReconciler(t, "test-agent", testIdentity("test-agent", testIdentityUID), testIdentity("other-agent", otherUID))
	reconcileOnce(t, r)
	pod := getPod(t, c)
	if pod == nil {
		t.Fatalf("no Pod: %s", runningReason(t, c))
	}

	// Admission refuses this while the Pod runs; a status report lagging a start can still let it in.
	setIdentity(t, c, "other-agent")
	reconcileOnce(t, r)
	if current := getPod(t, c); current != nil && current.UID == pod.UID && current.DeletionTimestamp.IsZero() {
		t.Fatal("the Pod acting as the earlier identity was kept")
	}
	reconcileOnce(t, r)
	reconcileOnce(t, r)
	if replacement := getPod(t, c); replacement == nil {
		t.Fatalf("no replacement Pod: %s", runningReason(t, c))
	}
	if subject := recordSubject(t, c); subject.Identity == nil || subject.Identity.UID != otherUID {
		t.Errorf("WorkloadCredential subject = %+v, want the newly named identity", subject)
	}
}
