package goproxyspike

import (
	"io"
	"net/http"
	"path/filepath"
	"testing"
	"time"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api"
	"github.com/bazelbuild/rules_go/go/runfiles"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/watch"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/envtest"
)

func TestEnvtestPolicyListWatchRevokesAnExistingH2Connection(t *testing.T) {
	asset, err := runfiles.Rlocation("envtest_kubernetes_linux_amd64/kube-apiserver")
	if err != nil {
		t.Fatal(err)
	}
	crd, err := runfiles.Rlocation("ducktape/agentplane/crds/manifests/crd-policybindings.yaml")
	if err != nil {
		t.Fatal(err)
	}
	existing := false
	environment := &envtest.Environment{BinaryAssetsDirectory: filepath.Dir(asset), CRDDirectoryPaths: []string{filepath.Dir(crd)}, DownloadBinaryAssets: false, ErrorIfCRDPathMissing: true, UseExistingCluster: &existing}
	config, err := environment.Start()
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := environment.Stop(); err != nil {
			t.Errorf("stop envtest: %v", err)
		}
	})
	scheme := runtime.NewScheme()
	if err := policyapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := corev1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	api, err := client.NewWithWatch(config, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	ctx := bounded(t)
	if err := api.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: "rules"}}); err != nil {
		t.Fatal(err)
	}
	l := newLab(t)
	set, binding := &l.snapshot.Sets[0], &l.snapshot.Bindings[0]
	if err := api.Create(ctx, set); err != nil {
		t.Fatal(err)
	}
	if err := api.Create(ctx, binding); err != nil {
		t.Fatal(err)
	}
	var sets policyapi.PolicySetList
	var bindings policyapi.PolicyBindingList
	if err := api.List(ctx, &sets, client.InNamespace("rules")); err != nil {
		t.Fatal(err)
	}
	if err := api.List(ctx, &bindings, client.InNamespace("rules")); err != nil {
		t.Fatal(err)
	}
	if sets.Items[0].Spec.Rules[0].CredentialRef.Name != "key" || sets.Items[0].Spec.Rules[0].Hosts[0] != fixtureHost {
		t.Fatal("shared Go type lost egress fields on real API roundtrip")
	}
	l.mu.Lock()
	l.snapshot.Sets = sets.Items
	l.snapshot.Bindings = bindings.Items
	l.mu.Unlock()
	events, err := api.Watch(ctx, &policyapi.PolicyBindingList{}, client.InNamespace("rules"), &client.ListOptions{Raw: &metav1.ListOptions{ResourceVersion: bindings.ResourceVersion}})
	if err != nil {
		t.Fatal(err)
	}
	defer events.Stop()
	upstream, counted := l.upstream(t, fixtureHost, true, http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.Write([]byte("admitted")) }))
	_, connection := l.tunnel(t, portOf(upstream), true)
	send := func() int {
		response, err := connection.RoundTrip(request(bounded(t), portOf(upstream), "/allowed", true, nil))
		if err != nil {
			t.Fatal(err)
		}
		io.Copy(io.Discard, response.Body)
		response.Body.Close()
		return response.StatusCode
	}
	if got := send(); got != 200 {
		t.Fatalf("before deletion=%d", got)
	}
	if err := api.Delete(ctx, &bindings.Items[0]); err != nil {
		t.Fatal(err)
	}
	select {
	case event := <-events.ResultChan():
		if event.Type != watch.Deleted {
			t.Fatalf("watch event=%v", event.Type)
		}
		revoked, ok := event.Object.(*policyapi.PolicyBinding)
		if !ok || revoked.UID != bindings.Items[0].UID {
			t.Fatalf("watch object=%T", event.Object)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("real API deletion watch did not arrive")
	}
	var current policyapi.PolicyBindingList
	if err := api.List(ctx, &current, client.InNamespace("rules")); err != nil {
		t.Fatal(err)
	}
	l.mu.Lock()
	l.snapshot.Bindings = current.Items
	l.mu.Unlock()
	if got := send(); got != 403 || counted.accepts.Load() != 1 {
		t.Fatalf("after deletion status=%d TCP=%d", got, counted.accepts.Load())
	}
	// Authentication above deliberately uses the controlled TokenReviews dependency.
	// envtest proves CRD admission/list/watch and socket revocation, not bound SA tokens.
}
