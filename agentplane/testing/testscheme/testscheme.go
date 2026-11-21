// Package testscheme builds the Kubernetes scheme the Environments tests share.
package testscheme

import (
	"testing"

	workloadapi "github.com/agentydragon/ducktape/agentplane/identity/credentials/api/v1alpha1"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	kubevirtv1 "kubevirt.io/api/core/v1"
)

// New returns a scheme with the built-in Kubernetes kinds and the Sandbox, policy, WorkloadCredential and KubeVirt kinds.
func New(t testing.TB) *runtime.Scheme {
	t.Helper()
	scheme := runtime.NewScheme()
	for _, add := range []func(*runtime.Scheme) error{
		clientgoscheme.AddToScheme, sandboxapi.AddToScheme, policyapi.AddToScheme, workloadapi.AddToScheme, kubevirtv1.AddToScheme,
	} {
		if err := add(scheme); err != nil {
			t.Fatal(err)
		}
	}
	return scheme
}
