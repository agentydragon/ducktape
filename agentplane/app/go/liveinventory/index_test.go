package liveinventory

import (
	"context"
	"testing"
	"time"

	egressapi "github.com/agentydragon/ducktape/agentplane/egress/api/v1alpha1"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/util/wait"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

func TestIndexSnapshotsWatchesAndBroadcastsChanges(t *testing.T) {
	kube, err := kubeapiserver.Start()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := kube.Stop(); err != nil {
			t.Error(err)
		}
	})

	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{
		corev1.AddToScheme, policyapi.AddToScheme, egressapi.AddToScheme,
	} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	objects, err := client.New(kube.Config, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	const namespace = "live-index"
	ctx := context.Background()
	if err := objects.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: namespace}}); err != nil {
		t.Fatal(err)
	}

	index, err := New(kube.Config, namespace)
	if err != nil {
		t.Fatal(err)
	}
	if index.health.Available() {
		t.Fatal("the index is available before its informers have synced")
	}
	runCtx, cancel := context.WithCancel(ctx)
	runDone := make(chan error, 1)
	go func() { runDone <- index.Run(runCtx) }()
	t.Cleanup(func() {
		cancel()
		if err := <-runDone; err != nil {
			t.Errorf("stop cache: %v", err)
		}
	})
	syncCtx, syncCancel := context.WithTimeout(ctx, 30*time.Second)
	defer syncCancel()
	if err := index.WaitForSync(syncCtx); err != nil {
		t.Fatal(err)
	}
	empty, err := index.Snapshot(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if !empty.Watch.Synced || !empty.Watch.Available || len(empty.PolicyBindings)+len(empty.PolicySets)+len(empty.EgressCredentials) != 0 {
		t.Fatalf("initial snapshot = %+v", empty)
	}

	first, unsubscribeFirst := index.Subscribe()
	defer unsubscribeFirst()
	second, unsubscribeSecond := index.Subscribe()
	defer unsubscribeSecond()
	policySet := &policyapi.PolicySet{ObjectMeta: metav1.ObjectMeta{Name: "egress", Namespace: namespace}, Spec: policyapi.PolicySetSpec{
		Rules: []policyapi.Rule{{Type: "egress", Hosts: []string{"example.com"}}},
	}}
	if err := objects.Create(ctx, policySet); err != nil {
		t.Fatal(err)
	}
	waitSignal(t, first)
	waitSignal(t, second)
	binding := &policyapi.PolicyBinding{ObjectMeta: metav1.ObjectMeta{Name: "workspace", Namespace: namespace}, Spec: policyapi.PolicyBindingSpec{
		Subject: policyapi.ServiceAccountRef{Namespace: namespace, Name: "workspace"}, PolicySets: []string{"egress"},
	}}
	if err := objects.Create(ctx, binding); err != nil {
		t.Fatal(err)
	}
	credential := &egressapi.EgressCredential{ObjectMeta: metav1.ObjectMeta{Name: "example", Namespace: namespace}, Spec: egressapi.EgressCredentialSpec{
		Description: "test credential",
		Source:      egressapi.CredentialSource{AuthenticatedWorkloadToken: &egressapi.AuthenticatedWorkloadTokenSource{}},
		Targets:     []egressapi.Target{{Method: egressapi.WholeValue, Header: "Authorization"}},
	}}
	if err := objects.Create(ctx, credential); err != nil {
		t.Fatal(err)
	}

	var snapshot Snapshot
	eventually(t, func() bool {
		var err error
		snapshot, err = index.Snapshot(ctx)
		return err == nil && len(snapshot.PolicyBindings) == 1 && len(snapshot.PolicySets) == 1 && len(snapshot.EgressCredentials) == 1
	})
	if snapshot.PolicyGeneration < 2 {
		t.Fatalf("Policy generation = %d, want a binding and PolicySet event", snapshot.PolicyGeneration)
	}
	if index.PolicyGeneration() != snapshot.PolicyGeneration {
		t.Fatalf("policy generation accessor = %d, snapshot = %d", index.PolicyGeneration(), snapshot.PolicyGeneration)
	}
	if !snapshot.Watch.Available {
		t.Fatalf("watch health = %+v", snapshot.Watch)
	}
	snapshot.PolicySets[0].Labels = map[string]string{"mutated": "caller"}
	snapshot, err = index.Snapshot(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, mutated := snapshot.PolicySets[0].Labels["mutated"]; mutated {
		t.Fatal("snapshot leaked a mutable cached PolicySet")
	}
}

func eventually(t *testing.T, check func() bool) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	defer cancel()
	if err := wait.PollUntilContextTimeout(ctx, 50*time.Millisecond, 25*time.Second, true, func(context.Context) (bool, error) {
		return check(), nil
	}); err != nil {
		t.Fatal(err)
	}
}

func waitSignal(t *testing.T, changes <-chan struct{}) {
	t.Helper()
	select {
	case _, open := <-changes:
		if !open {
			t.Fatal("change subscription was closed")
		}
	case <-time.After(10 * time.Second):
		t.Fatal("timed out waiting for informer change")
	}
}
