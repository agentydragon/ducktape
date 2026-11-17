package sandboxcontroller

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/environments/internal/runtimecredentials"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
)

// identityGate is why no workload may start, or nil: the Sandbox names no live Identity. A running workload
// is never stopped for it; with no workload, nothing runs, which admission reads to allow binding it.
func (r *Reconciler) identityGate(ctx context.Context, sandbox *sandboxapi.Sandbox, w workload) (*unready, error) {
	identity, err := r.readIdentity(ctx, sandbox)
	if err != nil {
		return nil, err
	}
	_, gate := runtimecredentials.Identity(sandbox, identity)
	blocked := credentialGate(gate)
	if blocked != nil && w.pod == nil && w.vm == nil && w.vmi == nil {
		blocked.stage = sandboxapi.StopStageStopped
	}
	return blocked, nil
}

func (r *Reconciler) readIdentity(ctx context.Context, sandbox *sandboxapi.Sandbox) (*policyapi.Identity, error) {
	if sandbox.Spec.Identity == nil {
		return nil, nil
	}
	var identity policyapi.Identity
	if err := r.Get(ctx, client.ObjectKey{Namespace: sandbox.Namespace, Name: sandbox.Spec.Identity.Name}, &identity); err != nil {
		return nil, client.IgnoreNotFound(err)
	}
	return &identity, nil
}

// owned is the metadata of an object named after the Sandbox and labeled with it.
func owned(sandbox *sandboxapi.Sandbox) metav1.ObjectMeta {
	return metav1.ObjectMeta{Name: sandbox.Name, Namespace: sandbox.Namespace, Labels: map[string]string{sandboxapi.SandboxLabel: sandbox.Name}}
}
