package runtimecredentials

import (
	"context"
	"reflect"
	"testing"

	workloadapi "github.com/agentydragon/ducktape/agentplane/identity/credentials/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/requester"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

type recordingRequester struct {
	requests []requester.Request
	revoked  []requester.Owner
	issued   requester.Issued
}

func (r *recordingRequester) Ensure(_ context.Context, request requester.Request) (requester.Credential, error) {
	r.requests = append(r.requests, request)
	return r.issued, nil
}

func (r *recordingRequester) Revoke(_ context.Context, owner requester.Owner) error {
	r.revoked = append(r.revoked, owner)
	return nil
}

// The decision boundary can accept a Pod using only snapshots and the credential requester;
// publishing the incarnation and persisting its binding remain the controller's responsibility.
func TestAcceptPodLeavesSnapshotsUnchanged(t *testing.T) {
	sandbox := &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Name: "sandbox", Namespace: "test", UID: "sandbox-uid", ResourceVersion: "7"},
		Spec:       sandboxapi.SandboxSpec{Identity: &sandboxapi.IdentityRef{Name: "agent"}},
	}
	identity := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Name: "agent", UID: "identity-uid"}}
	record := &workloadapi.WorkloadCredential{ObjectMeta: metav1.ObjectMeta{
		Name: sandbox.Name, Namespace: sandbox.Namespace,
		OwnerReferences: []metav1.OwnerReference{*metav1.NewControllerRef(sandbox, sandboxapi.GroupVersion.WithKind(sandboxapi.SandboxKind))},
	}, Spec: workloadapi.WorkloadCredentialSpec{Subject: workloadapi.Subject{Identity: &workloadapi.IdentitySubject{Name: identity.Name, UID: identity.UID}}}}
	const id = "0123456789abcdef0123456789abcdef"
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: sandbox.Name, UID: "pod-uid", ResourceVersion: "11", Labels: map[string]string{sandboxapi.EnvironmentRuntimeCredentialMetadataKey: id}}}
	beforeSandbox, beforePod, beforeIdentity, beforeRecord := sandbox.DeepCopy(), pod.DeepCopy(), identity.DeepCopy(), record.DeepCopy()
	credentials := &recordingRequester{issued: requester.Issued{ID: id, Accepted: true}}
	requests := Requests{Requester: credentials, Identity: identity, Record: record}
	outcome, err := requests.Decide(context.Background(), sandbox, Workload{Pod: pod}, true, nil)
	if err != nil {
		t.Fatal(err)
	}
	if accepted, ok := outcome.(AcceptPod); !ok || accepted.Issued.ID != id {
		t.Fatalf("outcome = %#v, want accepted Pod", outcome)
	}
	if !reflect.DeepEqual(sandbox, beforeSandbox) || !reflect.DeepEqual(pod, beforePod) || !reflect.DeepEqual(identity, beforeIdentity) || !reflect.DeepEqual(record, beforeRecord) {
		t.Fatal("decision changed an input snapshot")
	}
	if len(credentials.requests) != 1 || len(credentials.revoked) != 0 {
		t.Fatalf("credential requests = %+v, revocations = %+v", credentials.requests, credentials.revoked)
	}
	binding, ok := credentials.requests[0].Binding.(requester.Pod)
	if !ok || binding.UID != pod.UID || credentials.requests[0].Subject.UID != identity.UID {
		t.Fatalf("credential request lost UID fence: %+v", credentials.requests[0])
	}
}

func TestSubjectSnapshotsPreserveIdentityAndOwnerUIDFences(t *testing.T) {
	sandbox := &sandboxapi.Sandbox{
		ObjectMeta: metav1.ObjectMeta{Name: "sandbox", Namespace: "test", UID: "sandbox-uid"},
		Spec:       sandboxapi.SandboxSpec{Identity: &sandboxapi.IdentityRef{Name: "agent"}},
	}
	oldIdentity := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Name: "agent", UID: "old-identity-uid"}}
	recreatedIdentity := &policyapi.Identity{ObjectMeta: metav1.ObjectMeta{Name: "agent", UID: "new-identity-uid"}}
	record := &workloadapi.WorkloadCredential{ObjectMeta: metav1.ObjectMeta{
		Name: sandbox.Name, Namespace: sandbox.Namespace,
		OwnerReferences: []metav1.OwnerReference{*metav1.NewControllerRef(sandbox, sandboxapi.GroupVersion.WithKind(sandboxapi.SandboxKind))},
	}, Spec: workloadapi.WorkloadCredentialSpec{Subject: workloadapi.Subject{Identity: &workloadapi.IdentitySubject{Name: oldIdentity.Name, UID: oldIdentity.UID}}}}
	t.Run("running keeps deleted identity", func(t *testing.T) {
		credentials := &recordingRequester{}
		requests := Requests{Requester: credentials, Record: record}
		_, changed, _, err := requests.RequestIncarnation(context.Background(), sandbox, requester.Pod{Name: sandbox.Name, UID: "pod-uid"})
		if err != nil || changed || len(credentials.requests) != 1 || credentials.requests[0].Subject.UID != oldIdentity.UID {
			t.Fatalf("changed=%v, err=%v, requests=%+v", changed, err, credentials.requests)
		}
	})
	t.Run("running rejects same-name recreated identity", func(t *testing.T) {
		credentials := &recordingRequester{}
		requests := Requests{Requester: credentials, Identity: recreatedIdentity, Record: record}
		_, changed, _, err := requests.RequestIncarnation(context.Background(), sandbox, requester.Pod{Name: sandbox.Name, UID: "pod-uid"})
		if err != nil || !changed || len(credentials.requests) != 0 || len(credentials.revoked) != 0 {
			t.Fatalf("changed=%v, err=%v, requester=%+v", changed, err, credentials)
		}
	})
	t.Run("next start revokes before changing subject", func(t *testing.T) {
		credentials := &recordingRequester{}
		requests := Requests{Requester: credentials, Identity: recreatedIdentity, Record: record}
		_, gate, err := requests.RequestStart(context.Background(), sandbox)
		if err != nil || gate == nil || gate.Reason != "IdentityChanged" || len(credentials.requests) != 0 || len(credentials.revoked) != 1 {
			t.Fatalf("gate=%v, err=%v, requester=%+v", gate, err, credentials)
		}
		if holder := credentials.revoked[0].Holder.(requester.Controller); holder.Reference.UID != sandbox.UID {
			t.Fatalf("revocation owner = %+v", holder)
		}
	})
	t.Run("predecessor Sandbox record cannot select subject", func(t *testing.T) {
		predecessor := record.DeepCopy()
		predecessor.OwnerReferences[0].UID = "predecessor-sandbox-uid"
		credentials := &recordingRequester{}
		requests := Requests{Requester: credentials, Identity: recreatedIdentity, Record: predecessor}
		_, changed, _, err := requests.RequestIncarnation(context.Background(), sandbox, requester.Pod{Name: sandbox.Name, UID: "pod-uid"})
		if err != nil || changed || len(credentials.requests) != 1 || credentials.requests[0].Subject.UID != recreatedIdentity.UID {
			t.Fatalf("changed=%v, err=%v, requests=%+v", changed, err, credentials.requests)
		}
	})
}
