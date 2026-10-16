package sandboxcontroller

import (
	"context"
	"fmt"

	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	corev1ac "k8s.io/client-go/applyconfigurations/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api"
)

// ActionCallerLabel on a ServiceAccount is what admits its workload to the Action Service. It
// grants nothing alone: the account's PolicyBinding action rules decide. Must equal CALLER_LABEL in
// agentplane/action_service/policies/resources.py.
const ActionCallerLabel = "agentplane.allegedly.works/use-action-service"

// ensureIdentity keeps the Sandbox's own ServiceAccount and, while it names PolicySets, its one
// PolicyBinding. Both are controlled by the Sandbox, so deleting the Sandbox revokes them. It
// returns why no Pod may start yet; a running Pod is never stopped for it. A Pod does not wait for
// the binding's readers: a grant that lands after the Pod starts only means early requests are
// refused, the same as for a running Pod whose PolicySets change.
func (r *Reconciler) ensureIdentity(ctx context.Context, sandbox *sandboxapi.Sandbox) (*unready, error) {
	account := corev1ac.ServiceAccount(sandbox.Name, sandbox.Namespace).
		WithLabels(map[string]string{sandboxapi.SandboxLabel: sandbox.Name, ActionCallerLabel: "true"})
	if blocked, err := r.applyOwned(ctx, sandbox, account, "ServiceAccountConflict"); blocked != nil || err != nil {
		return blocked, err
	}
	return r.ensureBinding(ctx, sandbox)
}

func (r *Reconciler) ensureBinding(ctx context.Context, sandbox *sandboxapi.Sandbox) (*unready, error) {
	var binding policyapi.PolicyBinding
	err := r.Objects.Get(ctx, client.ObjectKeyFromObject(sandbox), &binding)
	if err != nil && !apierrors.IsNotFound(err) {
		return nil, err
	}
	if err == nil {
		if !metav1.IsControlledBy(&binding, sandbox) || !binding.DeletionTimestamp.IsZero() {
			return &unready{reason: "PolicyBindingConflict", message: fmt.Sprintf("PolicyBinding %s is foreign", binding.Name)}, nil
		}
		if len(sandbox.Spec.PolicySets) == 0 {
			return nil, client.IgnoreNotFound(r.Delete(ctx, &binding, client.Preconditions{UID: &binding.UID}))
		}
		// SSA cannot express removing this non-nullable optional CRD field when another
		// writer owns it. Clear drift with an optimistic patch before applying our intent.
		if binding.Spec.ExpiresAt != nil {
			before := binding.DeepCopy()
			binding.Spec.ExpiresAt = nil
			if err := r.Patch(ctx, &binding, client.MergeFromWithOptions(before, client.MergeFromWithOptimisticLock{})); err != nil {
				return nil, err
			}
		}
	}
	if len(sandbox.Spec.PolicySets) == 0 {
		return nil, nil
	}
	return r.applyOwned(ctx, sandbox, map[string]any{
		"apiVersion": policyapi.GroupVersion.String(), "kind": "PolicyBinding",
		"metadata": map[string]any{"name": sandbox.Name, "namespace": sandbox.Namespace, "labels": map[string]string{sandboxapi.SandboxLabel: sandbox.Name}},
		"spec": map[string]any{
			"subject":    map[string]any{"namespace": sandbox.Namespace, "name": sandbox.Name},
			"policySets": sandbox.Spec.PolicySets,
		},
	}, "PolicyBindingConflict")
}

// owned is the metadata of an object named after the Sandbox and labeled with it.
func owned(sandbox *sandboxapi.Sandbox) metav1.ObjectMeta {
	return metav1.ObjectMeta{Name: sandbox.Name, Namespace: sandbox.Namespace, Labels: map[string]string{sandboxapi.SandboxLabel: sandbox.Name}}
}
