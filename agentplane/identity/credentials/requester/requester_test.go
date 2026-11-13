package requester

import (
	"context"
	"os"
	"testing"

	workloadapi "github.com/agentydragon/ducktape/agentplane/identity/credentials/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/runtime/schema"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

const testNamespace = "test-requests"

var (
	objects client.Client
	sandbox = schema.GroupVersionKind{Group: "agentplane.allegedly.works", Version: "v1alpha1", Kind: "Sandbox"}
)

func TestMain(m *testing.M) {
	os.Exit(kubeapiserver.Run(m, func(server *kubeapiserver.Server) error {
		scheme := runtime.NewScheme()
		for _, add := range []func(*runtime.Scheme) error{corev1.AddToScheme, workloadapi.AddToScheme} {
			if err := add(scheme); err != nil {
				return err
			}
		}
		var err error
		if objects, err = client.New(server.Config, client.Options{Scheme: scheme}); err != nil {
			return err
		}
		return objects.Create(context.Background(), &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: testNamespace}})
	}))
}

// The owner is data to the requester: no Sandbox exists here.
func owner(name string, uid types.UID) Owner {
	return OwnedBy(&metav1.ObjectMeta{Namespace: testNamespace, Name: name, UID: uid}, sandbox)
}

func controllerUID(owner Owner) types.UID { return owner.Holder.(Controller).Reference.UID }

func newRequester(t *testing.T) Requester {
	t.Helper()
	requester, err := New(objects, objects)
	if err != nil {
		t.Fatal(err)
	}
	return requester
}

func ensure(t *testing.T, requester Requester, request Request) Credential {
	t.Helper()
	credential, err := requester.Ensure(t.Context(), request)
	if err != nil {
		t.Fatal(err)
	}
	return credential
}

func record(t *testing.T, name string) *workloadapi.WorkloadCredential {
	t.Helper()
	var record workloadapi.WorkloadCredential
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: testNamespace, Name: name}, &record); err != nil {
		t.Fatal(err)
	}
	return &record
}

// issue writes what the issuer would for the record's current generation.
func issue(t *testing.T, record *workloadapi.WorkloadCredential, id string, accepted *workloadapi.Binding, ready metav1.ConditionStatus, reason string) {
	t.Helper()
	record.Status.ObservedGeneration = record.Generation
	record.Status.CredentialID = id
	record.Status.URI = "spiffe://agentplane/" + testNamespace + "/" + record.Name + "/" + id
	record.Status.SecretName = "svid-" + record.Name + "-" + id
	record.Status.AcceptedBinding = accepted
	meta.SetStatusCondition(&record.Status.Conditions, metav1.Condition{
		Type: workloadapi.ConditionReady, Status: ready, Reason: reason, Message: "test " + reason, ObservedGeneration: record.Generation,
	})
	if err := objects.Status().Update(t.Context(), record); err != nil {
		t.Fatal(err)
	}
}

const (
	firstID  = "0123456789abcdef0123456789abcdef"
	secondID = "fedcba9876543210fedcba9876543210"
	firstPod = types.UID("11111111-1111-4111-8111-111111111111")
)

func TestARequestIsPendingUntilIssuedForItsCurrentSpec(t *testing.T) {
	requester := newRequester(t)
	request := Request{Owner: owner("test-lifecycle", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"), Subject: Identity{Name: "test-agent"}}
	if credential := ensure(t, requester, request); credential != (Pending{Reason: "Requested", Message: "WorkloadCredential test-requests/test-lifecycle is waiting for issuance"}) {
		t.Fatalf("first Ensure = %+v", credential)
	}
	created := record(t, "test-lifecycle")
	if controller := metav1.GetControllerOf(created); controller == nil || controller.UID != controllerUID(request.Owner) || controller.Kind != "Sandbox" ||
		created.Spec.Subject.Identity == nil || created.Spec.Subject.Identity.Name != "test-agent" || created.Spec.Binding != nil {
		t.Fatalf("created %+v", created)
	}

	issue(t, created, firstID, nil, metav1.ConditionFalse, "CertificatePending")
	if credential, ok := ensure(t, requester, request).(Pending); !ok || credential.Reason != "CertificatePending" {
		t.Fatalf("a not-Ready record = %+v", credential)
	}
	issue(t, created, firstID, nil, metav1.ConditionTrue, "Issued")
	want := Issued{ID: firstID, URI: "spiffe://agentplane/test-requests/test-lifecycle/" + firstID, SecretName: "svid-test-lifecycle-" + firstID}
	if credential := ensure(t, requester, request); credential != want {
		t.Fatalf("issued = %+v", credential)
	}

	// Binding the incarnation is a new generation: the old status no longer answers.
	request.Binding = Pod{Name: "test-first-pod", UID: firstPod}
	if _, ok := ensure(t, requester, request).(Pending); !ok {
		t.Fatal("a new binding answered with the previous generation's status")
	}
	bound := record(t, "test-lifecycle")
	if bound.Spec.Binding == nil || bound.Spec.Binding.PodUID != firstPod || *bound.Spec.Pod != (workloadapi.PodRef{Name: "test-first-pod", UID: firstPod}) {
		t.Fatalf("binding not written: %+v", bound.Spec)
	}
	if _, ok := ensure(t, requester, request).(Pending); !ok {
		t.Fatal("a generation the issuer has not observed answered")
	}
	issue(t, bound, firstID, &workloadapi.Binding{PodUID: firstPod}, metav1.ConditionTrue, "Issued")
	want.Accepted = true
	if credential := ensure(t, requester, request); credential != want {
		t.Fatalf("accepted = %+v", credential)
	}

	// The subject never moves under the same incarnation; the CRD refuses it.
	request.Subject = Identity{Name: "test-other-agent"}
	if _, err := requester.Ensure(t.Context(), request); !apierrors.IsInvalid(err) {
		t.Fatalf("re-pointing a running incarnation: %v", err)
	}
	request.Binding = Pod{Name: "test-second-pod", UID: "22222222-2222-4222-8222-222222222222"}
	if _, ok := ensure(t, requester, request).(Pending); !ok {
		t.Fatal("a new incarnation answered before issuance")
	}
	// The issuer rotated: the accepted binding of the old ID does not carry to the new one.
	issue(t, record(t, "test-lifecycle"), secondID, nil, metav1.ConditionTrue, "Issued")
	if credential, ok := ensure(t, requester, request).(Issued); !ok || credential.Accepted || credential.ID != secondID {
		t.Fatalf("after rotation = %+v", credential)
	}
}

func TestAnEarlierOwnersRecordIsReplacedAndAForeignOneKept(t *testing.T) {
	requester := newRequester(t)
	earlier := Request{Owner: owner("test-reused", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"), Subject: Identity{Name: "test-agent"}}
	ensure(t, requester, earlier)
	later := earlier
	later.Owner = owner("test-reused", "cccccccc-cccc-4ccc-8ccc-cccccccccccc")
	if credential, ok := ensure(t, requester, later).(Pending); !ok || credential.Reason != "PredecessorDeleting" {
		t.Fatalf("an earlier owner's record = %+v", credential)
	}
	ensure(t, requester, later)
	if controller := metav1.GetControllerOf(record(t, "test-reused")); controller.UID != controllerUID(later.Owner) {
		t.Fatalf("the record is still the earlier owner's: %+v", controller)
	}

	foreign := Request{
		Owner:   OwnedBy(&metav1.ObjectMeta{Namespace: testNamespace, Name: "test-reused", UID: "dddddddd-dddd-4ddd-8ddd-dddddddddddd"}, corev1.SchemeGroupVersion.WithKind("Pod")),
		Subject: Identity{Name: "test-agent"},
	}
	if credential, ok := ensure(t, requester, foreign).(Pending); !ok || credential.Reason != "RecordConflict" {
		t.Fatalf("another kind's record = %+v", credential)
	}
	if err := requester.Revoke(t.Context(), foreign.Owner); err != nil {
		t.Fatal(err)
	}
	if controller := metav1.GetControllerOf(record(t, "test-reused")); controller.UID != controllerUID(later.Owner) {
		t.Fatalf("a foreign request changed the record: %+v", controller)
	}

	if err := requester.Revoke(t.Context(), later.Owner); err != nil {
		t.Fatal(err)
	}
	err := objects.Get(t.Context(), types.NamespacedName{Namespace: testNamespace, Name: "test-reused"}, &workloadapi.WorkloadCredential{})
	if !apierrors.IsNotFound(err) {
		t.Fatalf("revoked record survived: %v", err)
	}
	if err := requester.Revoke(t.Context(), later.Owner); err != nil {
		t.Fatalf("revoking a gone record: %v", err)
	}
}

// An owner of several workloads, as a ServiceAccount is of its boxes, holds one record per
// workload, each named after it; revoking one leaves the others.
func TestAnOwnerHoldsARecordPerWorkloadName(t *testing.T) {
	requester := newRequester(t)
	account := OwnedBy(&metav1.ObjectMeta{Namespace: testNamespace, Name: "test-box-caller", UID: "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"},
		corev1.SchemeGroupVersion.WithKind("ServiceAccount"))
	first, second := account, account
	first.Name, second.Name = "test-box-caller-first", "test-box-caller-second"
	for _, box := range []Owner{first, second} {
		ensure(t, requester, Request{Owner: box, Subject: Identity{Name: "test-box-caller"}})
		if controller := metav1.GetControllerOf(record(t, box.Name)); controller == nil || controller.UID != controllerUID(account) {
			t.Fatalf("record %s is not the account's: %+v", box.Name, controller)
		}
	}
	if err := requester.Revoke(t.Context(), first); err != nil {
		t.Fatal(err)
	}
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: testNamespace, Name: first.Name}, &workloadapi.WorkloadCredential{}); !apierrors.IsNotFound(err) {
		t.Fatalf("revoked record survived: %v", err)
	}
	record(t, second.Name)
}

// An unowned record is told from another's by its labels alone: a same-named record of another
// holder, or of another owner's labels, is never adopted, changed or revoked.
func TestAnUnownedRecordIsKnownByItsLabels(t *testing.T) {
	requester := newRequester(t)
	labelled := func(uid string) Owner {
		return Owner{Namespace: testNamespace, Name: "test-unowned", Holder: Unowned{Labels: map[string]string{"test.example/owner-uid": uid}}}
	}
	first := Request{Owner: labelled("first"), Subject: Identity{Name: "test-agent", UID: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"}}
	ensure(t, requester, first)
	created := record(t, "test-unowned")
	if len(created.OwnerReferences) != 0 || created.Labels["test.example/owner-uid"] != "first" || created.Labels[UnownedLabel] != "true" {
		t.Fatalf("created %+v", created.ObjectMeta)
	}
	issue(t, created, firstID, nil, metav1.ConditionTrue, "Issued")
	if credential, ok := ensure(t, requester, first).(Issued); !ok || credential.ID != firstID {
		t.Fatalf("the owner's record = %+v", credential)
	}

	successor := Request{Owner: labelled("successor"), Subject: Identity{Name: "test-agent", UID: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"}}
	controlled := Request{Owner: owner("test-unowned", "cccccccc-cccc-4ccc-8ccc-cccccccccccc"), Subject: first.Subject}
	for name, other := range map[string]Request{"other labels": successor, "a controller": controlled} {
		if credential, ok := ensure(t, requester, other).(Pending); !ok || credential.Reason != "RecordConflict" {
			t.Errorf("%s: %+v, want RecordConflict", name, credential)
		}
		if err := requester.Revoke(t.Context(), other.Owner); err != nil {
			t.Fatal(err)
		}
	}
	if kept := record(t, "test-unowned"); kept.UID != created.UID || kept.Spec.Subject.Identity.UID != first.Subject.UID {
		t.Fatalf("another holder changed the record: %+v", kept)
	}

	if err := requester.Revoke(t.Context(), first.Owner); err != nil {
		t.Fatal(err)
	}
	if err := objects.Get(t.Context(), types.NamespacedName{Namespace: testNamespace, Name: "test-unowned"}, &workloadapi.WorkloadCredential{}); !apierrors.IsNotFound(err) {
		t.Fatalf("revoked record survived: %v", err)
	}
}

// A record a controller owns is not an unowned owner's, even with its labels.
func TestAControlledRecordIsNotAnUnownedOnes(t *testing.T) {
	requester := newRequester(t)
	controlled := Request{Owner: owner("test-controlled", "dddddddd-dddd-4ddd-8ddd-dddddddddddd"), Subject: Identity{Name: "test-agent"}}
	ensure(t, requester, controlled)
	existing := record(t, "test-controlled")
	labels := map[string]string{"test.example/owner-uid": "first"}
	existing.Labels = map[string]string{"test.example/owner-uid": "first", UnownedLabel: "true"}
	if err := objects.Update(t.Context(), existing); err != nil {
		t.Fatal(err)
	}
	unowned := Request{Owner: Owner{Namespace: testNamespace, Name: "test-controlled", Holder: Unowned{Labels: labels}}, Subject: Identity{Name: "test-agent"}}
	if credential, ok := ensure(t, requester, unowned).(Pending); !ok || credential.Reason != "RecordConflict" {
		t.Fatalf("a controlled record = %+v, want RecordConflict", credential)
	}
}
