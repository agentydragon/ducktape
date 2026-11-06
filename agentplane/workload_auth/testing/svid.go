// Package workloadauthtesting creates live Sandbox credentials on an envtest API server.
package workloadauthtesting

import (
	"strings"
	"testing"

	transport "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/testing/kubeapiserver"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"github.com/google/uuid"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type Workload struct {
	Objects   client.Client
	Sandbox   *sandboxapi.Sandbox
	Principal auth.Principal
	URI       string
}

// NewWorkload records the controller-owned account and exact accepted Pod binding
// that a service's resolver must verify. No controller or kubelet is simulated.
func NewWorkload(t *testing.T, api *kubeapiserver.Server, namespace, name string) Workload {
	t.Helper()
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{corev1.AddToScheme, sandboxapi.AddToScheme} {
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
	credential := strings.ReplaceAll(uuid.NewString(), "-", "")
	sandbox.Status = sandboxapi.SandboxStatus{
		RunnerCredential: &sandboxapi.RunnerCredential{ID: credential, AcceptedID: credential, Kind: sandboxapi.RunnerCredentialKindPod, PodUID: string(pod.UID)},
		Incarnation:      &sandboxapi.Incarnation{Name: pod.Name, UID: string(pod.UID)},
	}
	if err := objects.Status().Update(t.Context(), sandbox); err != nil {
		t.Fatal(err)
	}
	var account corev1.ServiceAccount
	if err := objects.Get(t.Context(), client.ObjectKeyFromObject(sandbox), &account); err != nil {
		t.Fatal(err)
	}
	account.OwnerReferences = []metav1.OwnerReference{*metav1.NewControllerRef(sandbox, sandboxapi.GroupVersion.WithKind("Sandbox"))}
	if err := objects.Update(t.Context(), &account); err != nil {
		t.Fatal(err)
	}
	uri, err := transport.EnvironmentRuntimeURI(namespace, string(sandbox.UID), credential)
	if err != nil {
		t.Fatal(err)
	}
	return Workload{Objects: objects, Sandbox: sandbox, Principal: auth.Principal{Account: auth.Account{Namespace: namespace, Name: name}, ServiceAccountUID: string(account.UID), PodName: pod.Name, PodUID: string(pod.UID)}, URI: uri}
}
