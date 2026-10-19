package gateway

import (
	"context"
	"strings"
	"testing"
	"time"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	sandboxlease "github.com/agentydragon/ducktape/agentplane/sandboxes/lease"
	coordinationv1 "k8s.io/api/coordination/v1"
	discoveryv1 "k8s.io/api/discovery/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	kubernetesfake "k8s.io/client-go/kubernetes/fake"
)

func TestConnectionRecordsRejectForeignObjects(t *testing.T) {
	sandbox := SandboxIdentity{Name: testSandbox, UID: testSandboxUID}
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
					_, err = records.accept(context.Background(), sandbox, testPodUID, "boot")
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
