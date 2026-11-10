// Package workloadauthtesting creates live Sandbox workload credentials on an envtest API server.
package workloadauthtesting

import (
	"strings"
	"testing"

	transport "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	workloadapi "github.com/agentydragon/ducktape/agentplane/workload_identity/api/v1alpha1"
	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type Workload struct {
	Objects client.Client
	Sandbox *sandboxapi.Sandbox
	// Record is the Sandbox's WorkloadCredential, issued and accepted for the Pod.
	Record    *workloadapi.WorkloadCredential
	Principal auth.Principal
	URI       string
}

// NewWorkload records a Sandbox, its ServiceAccount, a Pod running as it, and the WorkloadCredential
// the Sandbox Controller requests and the issuer accepts for that Pod, which a service's resolver must
// verify. No controller, issuer or kubelet runs.
func NewWorkload(t *testing.T, api *kubeapiserver.Server, namespace, name string) Workload {
	t.Helper()
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{corev1.AddToScheme, sandboxapi.AddToScheme, workloadapi.AddToScheme, policyapi.AddToScheme} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	objects, err := client.New(api.Config, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	pod, err := api.Pod(t.Context(), auth.Account{Namespace: namespace, Name: name})
	if err != nil {
		t.Fatal(err)
	}
	sandbox := &sandboxapi.Sandbox{ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: name}, Spec: sandboxapi.SandboxSpec{Template: "test-template", Running: true}}
	if err := objects.Create(t.Context(), sandbox); err != nil {
		t.Fatal(err)
	}
	sandbox.Status = sandboxapi.SandboxStatus{
		RunnerCredential: &sandboxapi.RunnerCredential{Kind: sandboxapi.RunnerCredentialKindPod, PodUID: string(pod.UID)},
		Incarnation:      &sandboxapi.Incarnation{Name: pod.Name, UID: string(pod.UID)},
	}
	if err := objects.Status().Update(t.Context(), sandbox); err != nil {
		t.Fatal(err)
	}
	var account corev1.ServiceAccount
	if err := objects.Get(t.Context(), client.ObjectKeyFromObject(sandbox), &account); err != nil {
		t.Fatal(err)
	}
	record := &workloadapi.WorkloadCredential{
		ObjectMeta: metav1.ObjectMeta{
			Namespace: namespace, Name: name,
			OwnerReferences: []metav1.OwnerReference{*metav1.NewControllerRef(sandbox, sandboxapi.GroupVersion.WithKind(sandboxapi.SandboxKind))},
		},
		Spec: workloadapi.WorkloadCredentialSpec{
			Subject: workloadapi.Subject{ServiceAccount: name},
			Binding: &workloadapi.Binding{PodUID: pod.UID},
			Pod:     &workloadapi.PodRef{Name: pod.Name, UID: pod.UID},
		},
	}
	if err := objects.Create(t.Context(), record); err != nil {
		t.Fatal(err)
	}
	workload := Workload{Objects: objects, Sandbox: sandbox, Record: record}
	workload.URI = workload.Issue(t, strings.ReplaceAll(uuid.NewString(), "-", ""))
	workload.Principal = auth.Principal{Account: auth.Account{Namespace: namespace, Name: name}, ServiceAccountUID: string(account.UID), PodName: pod.Name, PodUID: string(pod.UID)}
	return workload
}

// Issue writes the status the issuer would for credential id, accepted for the record's binding,
// and returns the URI a certificate for it carries. Issuing another ID revokes the previous one.
func (w Workload) Issue(t *testing.T, id string) string {
	t.Helper()
	record := w.Record
	if err := w.Objects.Get(t.Context(), client.ObjectKeyFromObject(record), record); err != nil {
		t.Fatal(err)
	}
	uri, err := transport.WorkloadID{Namespace: record.Namespace, Name: record.Name, CredentialID: id}.URI()
	if err != nil {
		t.Fatal(err)
	}
	record.Status.ObservedGeneration = record.Generation
	record.Status.CredentialID, record.Status.URI, record.Status.SecretName = id, uri, "svid-"+record.Name+"-"+id
	record.Status.AcceptedBinding = record.Spec.Binding.DeepCopy()
	meta.SetStatusCondition(&record.Status.Conditions, metav1.Condition{
		Type: workloadapi.ConditionReady, Status: metav1.ConditionTrue, Reason: "Issued", Message: "test issued", ObservedGeneration: record.Generation,
	})
	if err := w.Objects.Status().Update(t.Context(), record); err != nil {
		t.Fatal(err)
	}
	return uri
}

// Revoke deletes the record, as deleting its Sandbox does once the garbage collector, which envtest
// does not run, follows the owner reference.
func (w Workload) Revoke(t *testing.T) {
	t.Helper()
	if err := w.Objects.Delete(t.Context(), w.Record); err != nil && !apierrors.IsNotFound(err) {
		t.Fatal(err)
	}
}

// Delete deletes the Sandbox and, as the garbage collector would, its record.
func (w Workload) Delete(t *testing.T) {
	t.Helper()
	if err := w.Objects.Delete(t.Context(), w.Sandbox); err != nil {
		t.Fatal(err)
	}
	w.Revoke(t)
}

// IdentityWorkload is a Workload whose record names an Identity in the policy namespace. Its
// Principal is still what a TokenReview of its Pod's token proves.
type IdentityWorkload struct {
	Workload
	Identity *policyapi.Identity
	// Subject is what a verifier given the policy namespace resolves its SVID to.
	Subject auth.IdentityPrincipal
}

// NewIdentityWorkload is NewWorkload with the record naming a new Identity in policyNamespace,
// which it creates if missing.
func NewIdentityWorkload(t *testing.T, api *kubeapiserver.Server, namespace, name, policyNamespace string) IdentityWorkload {
	t.Helper()
	workload := NewWorkload(t, api, namespace, name)
	ctx := t.Context()
	if err := workload.Objects.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: policyNamespace}}); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatal(err)
	}
	identity := &policyapi.Identity{
		ObjectMeta: metav1.ObjectMeta{Namespace: policyNamespace, Name: name},
		Spec:       policyapi.IdentitySpec{Description: "test agent " + name},
	}
	if err := workload.Objects.Create(ctx, identity); err != nil {
		t.Fatal(err)
	}
	// A record's subject changes only with a new binding, so the record is replaced.
	record := workload.Record
	if err := workload.Objects.Delete(ctx, record); err != nil {
		t.Fatal(err)
	}
	workload.Record = &workloadapi.WorkloadCredential{
		ObjectMeta: metav1.ObjectMeta{Namespace: record.Namespace, Name: record.Name, OwnerReferences: record.OwnerReferences},
		Spec:       *record.Spec.DeepCopy(),
	}
	workload.Record.Spec.Subject = workloadapi.Subject{Identity: &workloadapi.IdentitySubject{Name: identity.Name, UID: identity.UID}}
	if err := workload.Objects.Create(ctx, workload.Record); err != nil {
		t.Fatal(err)
	}
	workload.URI = workload.Issue(t, strings.ReplaceAll(uuid.NewString(), "-", ""))
	return IdentityWorkload{
		Workload: workload,
		Identity: identity,
		Subject: auth.IdentityPrincipal{
			Identity: auth.Identity{Name: identity.Name, UID: string(identity.UID)},
			Workload: auth.Workload{Namespace: namespace, Name: name, Owner: auth.ControllerOwner{
				APIVersion: sandboxapi.GroupVersion.String(), Kind: sandboxapi.SandboxKind,
				Name: workload.Sandbox.Name, UID: string(workload.Sandbox.UID),
			}},
			PodName: workload.Principal.PodName, PodUID: workload.Principal.PodUID,
		},
	}
}
