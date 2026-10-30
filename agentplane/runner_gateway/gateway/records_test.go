package gateway

import (
	"context"
	"strings"
	"testing"
	"time"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	sandboxlease "github.com/agentydragon/ducktape/agentplane/sandboxes/lease"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	coordinationv1 "k8s.io/api/coordination/v1"
	discoveryv1 "k8s.io/api/discovery/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	kubernetesfake "k8s.io/client-go/kubernetes/fake"
)

const (
	testNamespace  = "test-sandboxes"
	testSandboxUID = "test-sandbox-uid-a"
	testPodUID     = "test-runner-pod-uid-a"
)

func TestConnectionRecordsRejectForeignObjects(t *testing.T) {
	sandbox := sandboxreader.Identity{Name: testSandbox, UID: testSandboxUID}
	owner := sandboxOwnerReference(sandbox)
	cases := []struct {
		name   string
		mutate func(*metav1.ObjectMeta)
	}{
		{"unowned", func(m *metav1.ObjectMeta) { m.OwnerReferences = nil }},
		{"previous Sandbox", func(m *metav1.ObjectMeta) { m.OwnerReferences[0].UID = "previous-sandbox-uid" }},
		{"different API", func(m *metav1.ObjectMeta) { m.OwnerReferences[0].APIVersion = "other.example/v1" }},
		{"different kind", func(m *metav1.ObjectMeta) { m.OwnerReferences[0].Kind = "Other" }},
		{"different name", func(m *metav1.ObjectMeta) { m.OwnerReferences[0].Name = "other-sandbox" }},
		{"multiple owners", func(m *metav1.ObjectMeta) { m.OwnerReferences = append(m.OwnerReferences, owner) }},
		{"deleting", func(m *metav1.ObjectMeta) { m.DeletionTimestamp = &metav1.Time{Time: time.Now()} }},
	}
	for _, kind := range []string{"Lease", "EndpointSlice"} {
		for _, test := range cases {
			t.Run(kind+"/"+test.name, func(t *testing.T) {
				metadata := metav1.ObjectMeta{Namespace: testNamespace, OwnerReferences: []metav1.OwnerReference{owner}}
				test.mutate(&metadata)
				var object runtime.Object
				if kind == "Lease" {
					metadata.Name = sandbox.Name + sandboxlease.RunnerLeaseSuffix
					object = &coordinationv1.Lease{ObjectMeta: metadata}
				} else {
					metadata.Name = sandbox.Name + sandboxapi.ControlServiceSuffix
					object = &discoveryv1.EndpointSlice{ObjectMeta: metadata}
				}
				kube := kubernetesfake.NewSimpleClientset(object)
				records, err := newConnectionRecords(kube.CoordinationV1(), kube.DiscoveryV1(), testNamespace,
					connectionHolder{name: "gateway", ip: "10.0.0.1", port: sandboxapi.ControlServicePort})
				if err != nil {
					t.Fatal(err)
				}
				if kind == "Lease" {
					_, err = records.claim(context.Background(), sandbox, testPodUID)
				} else {
					err = records.pointControlEndpoints(context.Background(), sandbox, 1)
				}
				if err == nil || !strings.Contains(err.Error(), "refusing") {
					t.Fatalf("expected ownership refusal, got %v", err)
				}
				for _, action := range kube.Actions() {
					if action.GetVerb() != "get" {
						t.Fatalf("refusal mutated Kubernetes state: %s %s", action.GetVerb(), action.GetResource().Resource)
					}
				}
			})
		}
	}
}

func TestConnectionRenewPublishesAndClearsAutosuspendBlockers(t *testing.T) {
	kube := kubernetesfake.NewSimpleClientset()
	identity := sandboxreader.Identity{Name: testSandbox, UID: testSandboxUID}
	records, err := newConnectionRecords(kube.CoordinationV1(), kube.DiscoveryV1(), testNamespace,
		connectionHolder{name: "test-gateway", ip: "192.0.2.10", port: sandboxapi.ControlServicePort})
	if err != nil {
		t.Fatal(err)
	}
	epoch, err := records.claim(context.Background(), identity, testPodUID)
	if err != nil {
		t.Fatal(err)
	}
	sessionID := "session-a"
	busy := &sandboxlease.RunnerFacts{AutosuspendBlockers: []sandboxapi.AutosuspendBlocker{{
		Reason: sandboxapi.AutosuspendBlockerActiveTurn, SessionID: &sessionID,
	}}}
	if err := records.renew(context.Background(), identity, epoch, time.Now(), busy); err != nil {
		t.Fatal(err)
	}
	name := testSandbox + sandboxlease.RunnerLeaseSuffix
	lease, err := kube.CoordinationV1().Leases(testNamespace).Get(context.Background(), name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if lease.Annotations[sandboxlease.AutosuspendBlockersAnnotation] == "" {
		t.Fatal("busy runner facts were not published on the connection Lease")
	}
	if err := records.renew(context.Background(), identity, epoch, time.Now(), &sandboxlease.RunnerFacts{}); err != nil {
		t.Fatal(err)
	}
	lease, err = kube.CoordinationV1().Leases(testNamespace).Get(context.Background(), name, metav1.GetOptions{})
	if err != nil {
		t.Fatal(err)
	}
	if _, exists := lease.Annotations[sandboxlease.AutosuspendBlockersAnnotation]; exists {
		t.Fatalf("idle runner retained blocker annotation: %s", lease.Annotations[sandboxlease.AutosuspendBlockersAnnotation])
	}
}

func TestObservationClaimsPreserveVerifiedContact(t *testing.T) {
	ctx := context.Background()
	kube := kubernetesfake.NewSimpleClientset()
	identity := sandboxreader.Identity{Name: testSandbox, UID: testSandboxUID}
	records, err := newConnectionRecords(kube.CoordinationV1(), kube.DiscoveryV1(), testNamespace,
		connectionHolder{name: "gateway", ip: "192.0.2.10", port: sandboxapi.ControlServicePort})
	if err != nil {
		t.Fatal(err)
	}
	epoch, err := records.claim(ctx, identity, testPodUID)
	if err != nil {
		t.Fatal(err)
	}
	read := func() *coordinationv1.Lease {
		t.Helper()
		lease, err := kube.CoordinationV1().Leases(testNamespace).Get(ctx, testSandbox+sandboxlease.RunnerLeaseSuffix, metav1.GetOptions{})
		if err != nil {
			t.Fatal(err)
		}
		return lease
	}
	if sandboxlease.LastConnectedPodUID(read().Annotations) != "" {
		t.Fatal("claim counted as contact")
	}
	if err := records.recordBootID(ctx, identity, epoch, "boot"); err != nil {
		t.Fatal(err)
	}
	for _, uid := range []string{testPodUID, "replacement-pod"} {
		epoch, err = records.claim(ctx, identity, uid)
		if err != nil {
			t.Fatal(err)
		}
		lease := read()
		if lease.Annotations[sandboxlease.RunnerBootIDAnnotation] != "" {
			t.Fatal("claim retained current boot ID")
		}
		if sandboxlease.LastConnectedPodUID(lease.Annotations) != testPodUID {
			t.Fatal("claim erased verified contact")
		}
	}
	if err := records.recordBootID(ctx, identity, epoch, "replacement-boot"); err != nil {
		t.Fatal(err)
	}
	if sandboxlease.LastConnectedPodUID(read().Annotations) != "replacement-pod" {
		t.Fatal("verified replacement did not update contact")
	}
}
